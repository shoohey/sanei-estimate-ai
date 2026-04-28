"""現調シート・図面の抽出プロンプト定義（精度重視・住宅/法人分岐対応）

このモジュールは2026-04-20の打ち合わせ要望を受けて作成された:
- 手書き現調シートのPDF読み取り精度を上げたい
- 住宅(低圧/<10kW)と法人(高圧)で別プロンプトに分岐したい

提供する定数:
- COMMERCIAL_EXTRACTION_PROMPT: 法人・高圧用の高精度プロンプト
- RESIDENTIAL_EXTRACTION_PROMPT: 住宅・低圧用プロンプト
- CLASSIFICATION_PROMPT: 文書を commercial / residential / unknown に分類するプロンプト

すべてのプロンプトは models/survey_data.py の SurveyData スキーマと互換のJSONを返す。
"""

__all__ = [
    "COMMERCIAL_EXTRACTION_PROMPT",
    "RESIDENTIAL_EXTRACTION_PROMPT",
    "CLASSIFICATION_PROMPT",
]


# ---------------------------------------------------------------------------
# 共通ヒューリスティック（手書き判別ガイド・典型値・自己整合チェック）
# ---------------------------------------------------------------------------
_COMMON_HEURISTICS = """
【手書き数字の判別ガイド（最優先で参照）】
- 「6」と「0」: 6は上部に開口部があり下部が膨らむ。0は完全に閉じた縦長の楕円。
  迷ったら文脈を考慮: モジュール出力なら 400/460/580/600/660 など典型値に寄せる。
- 「1」と「7」: 1は縦の直線（上端のセリフが小さい）。7は上部に明確な横線があり、
  日本人の手書きは中央に横棒（ヨーロッパ式）が入ることもある。文末が斜めなら7。
- 「3」と「8」: 3は右側のみが開いた二重曲線。8は上下とも閉じ、中心でクロスする。
  中心の交差点が明瞭なら8、上下が右に膨らむ書き分けなら3。
- 「4」と「9」: 4は左上の角が直角的（オープン4・クローズ4どちらでも左下が空く）。
  9は丸い頭＋まっすぐな尾。尾が右に流れていれば9、垂直で交差していれば4。
- 「2」と「Z」: 2は上部が曲線で、底辺が水平に伸びる。Zは全て直線で構成。
  数字記入欄では基本2と判定してよい。
- 「5」と「S」: 5は上部に水平線があり中央でカクッと曲がる。Sは滑らかな2重曲線。
  数字記入欄なら5。
- 全角数字（０〜９）は半角（0〜9）に変換して返す。

【数字桁数の文脈判断】
- module_output_w（モジュール出力W）: 通常 300〜720W。次の典型値のいずれかに寄せる:
  400, 410, 430, 450, 460, 480, 500, 540, 550, 580, 600, 660, 670, 680, 700, 720
  これらから外れる読み取り結果は再度手書き文字を確認すること。
- planned_panels（設置枚数）: 通常 30〜2000枚。100枚単位や288/376/66/40枚など
  施工単位の倍数が多い。1桁・5桁は誤読の可能性が高い。
- pv_capacity_kw（PV容量kW）: 法人は 10〜500kW、住宅は 3〜10kW が一般的。
- separation_ns_mm / separation_ew_mm: 通常 1500〜5000mm。手書き「3m」は3000、
  「2.5m」は2500、「300cm」は3000、「3000mm」は3000。返り値は必ずmm。

【メーカー候補リスト（読み取りはこれらから優先選択）】
和文表記:
  シャープ, パナソニック, 京セラ, 三菱電機, ハンファQセルズ, ネクストエナジー,
  株式会社SILFINJAPAN, シルフィンジャパン, 長州産業, ソーラーフロンティア
英文表記:
  Canadian Solar, Longi, JA Solar, Jinko, Trina, Q CELLS, Sharp, Panasonic,
  NextEnergy, SILFIN JAPAN, REC, LG, Hyundai, Risen
- 「NextEnergy」「ネクストエナジー」「NER...」型番は同一メーカー（NextEnergy）に統一。
- 「SILFIN」「SILFINJAPAN」「シルフィンジャパン」「株式会社SILFINJAPAN」は同一。
  正式名称「株式会社SILFINJAPAN」を優先。
- 「シャープ」「Sharp」「SHARP」型番が「NU-...」で始まればシャープ。

【PV容量の自己整合チェック（必須）】
pv_capacity_kw ≈ module_output_w × planned_panels / 1000 が必ず成立する。
例:
  400W × 376枚 / 1000 = 150.40kW （三精産業様）
  460W × 66枚 / 1000  = 30.36kW  （尾島商店様）
  580W × 40枚 / 1000  = 23.20kW  （新座市第二老人福祉センター様）
読み取った3つの値で計算して、5%以上ズレていたらどれかを誤読している。
最も信頼できるフィールドを優先し、他はそれに合わせて再確認すること。
誤差が10%超なら計算値で補正し extraction_warnings に明記する。

【接地種類の複数選択（重要）】
- A種・C種・D種は **同時に複数選択されることがあり得る**（A種とD種の両方に丸など）。
- 単一フィールド ground_type には主たる1つを返す（A > C > D の優先で「A」）。
- 複数選択されている場合は handwritten_notes か single_line_diagram_note に
  「接地: A,D（A種とD種を併用）」のように明記すること。
- 完全に丸で囲まれていなくても、半円・U字・部分的な弧でも選択意図と判断する。
- 取り消し線（××）が引かれた丸は無視。書き直しがある場合は最も濃い丸を採用。

【離隔距離（separation_*_mm）】
- 単位はm/cm/mm混在で記載される。返り値は必ずmm（整数）。
  「3m」→3000、「2.5m」→2500、「300cm」→3000、「3000mm」→3000、「3,020」→3020。
- 北南方向(NS)と東西方向(EW)の両方を同じ単位で読み取る。
- 数字に「,」が含まれる場合（3,020）はカンマを除去して数値化。

【郵便番号】
- 必ず「XXX-XXXX」形式の7桁。住所中の「〒」マークの直後を最優先で読む。
- 〒なしでも住所先頭に7桁数字があれば郵便番号として抽出。
- 半角ハイフンで連結。全角数字は半角化。
- 例: 「〒530-0001 大阪府...」→ postal_code: "530-0001", address: "大阪府..."

【日付（令和年表記対応）】
- 西暦4桁優先: 2025/09/29 → "2025/09/29"
- 令和: R7=2025, R8=2026, R9=2027, R6=2024, R5=2023。
  例: 「R7.9.29」→「2025/09/29」、「令和7年9月29日」→「2025/09/29」。
- 月日のみ（例: 「9/29」）の場合は調査日が今年と推定して補完。

【選択肢の認識（丸囲み・チェックマーク・レ点・×印）】
- 完全な丸でなくても、部分円・半円・U字・弧でも「選択中」と判断。
- チェックマーク(✓)、レ点(レ)、×印(該当に×)、塗りつぶし(■)は全て「選択中」。
- 取り消し線が引かれた選択肢は **未選択** と判断。
- 同じ項目に複数の印があり書き直しの跡があれば、最も濃く・最後に書かれたものを採用。

【書類タイプ別の主要情報源】
- 現調シート1ページ目: 案件名・所在地・調査日・天気・調査者・モジュール仕様・PV容量・
  接地種類・VT/CT・PCS位置・BT位置・離隔距離・最終確認欄
- 現調シート2ページ目（別紙）: クレーン有無・足場設置位置・電柱番号・1号柱・
  配管/配線ルート・キュービクル位置・BT設置位置・引込柱・メーター番号・手書きメモ
- 配管図: 配管延長(m)・ケーブルラック長(m)・PCS設置位置・接地電極位置 →
  数値は handwritten_notes に「配管延長: 45m, ケーブルラック: 30m」のように追加
- 単線結線図: VT/CT有無・接地種類・変圧器容量(kVA) →
  「Tr容量: 300kVA」のように handwritten_notes に追加
- 配置図/屋根伏図: パネル配置・離隔距離・パネル枚数

【信頼度（field_confidences）の付与基準】
- "high": 印字された値、または手書きでも数字が完全に明瞭で迷いなし
- "medium": 手書きで読めるが似た字形（6/0、1/7など）に若干の不安あり
- "low": 不明瞭、複数候補あり、または読み取り不能。warningsにも明記する
"""


# ---------------------------------------------------------------------------
# Few-shot 事例（実在の正解データから生成）
# ---------------------------------------------------------------------------
_COMMERCIAL_FEW_SHOT_EXAMPLES = """
【法人・高圧の読み取り Few-shot 事例（実在案件の正解）】

### 例1: 法人・高圧（三精産業様 稲敷工場）
入力: 現調シート（手書き）+ 配管図 + 単線結線図
読み取り結果（正解）:
```json
{
  "project": {
    "project_name": "三精産業株式会社様 稲敷工場 自家消費太陽光設置工事",
    "address": "茨城県稲敷市高田",
    "postal_code": "",
    "survey_date": "2025/09/29",
    "weather": "晴れ",
    "surveyor": "高橋和浩"
  },
  "equipment": {
    "module_maker": "株式会社SILFINJAPAN",
    "module_model": "SFJ-400-EWH",
    "module_output_w": 400,
    "planned_panels": 376,
    "pv_capacity_kw": 150.4,
    "design_status": "確定"
  },
  "high_voltage": {
    "building_drawing": true,
    "single_line_diagram": true,
    "single_line_diagram_note": "接地: A,D（A種とD種を併用）",
    "ground_type": "A",
    "c_installation": "可",
    "vt_available": true,
    "ct_available": true,
    "pcs_location": "屋外",
    "bt_space": "屋外",
    "tr_capacity": "十分",
    "separation_ns_mm": 2543,
    "separation_ew_mm": 2870
  },
  "supplementary": {
    "handwritten_notes": "接地はA種とD種を併用。離隔NS=2543mm, EW=2870mm。"
  }
}
```
ポイント:
- 接地種類でA種とD種の両方に丸が付いている → ground_type は "A" を返し、
  single_line_diagram_note に "A,D" と明記
- module_output_w=400, planned_panels=376, pv_capacity_kw=150.4 で自己整合
  （400 × 376 / 1000 = 150.4 で完全一致）
- メーカー名は「株式会社SILFINJAPAN」が正式名称（型番先頭の SFJ- とも整合）

### 例2: 法人・高圧（株式会社尾島商店 本社）
入力: 現調シート（手書き）+ 屋上配置図
読み取り結果（正解）:
```json
{
  "project": {
    "project_name": "株式会社尾島商店 本社 自家消費太陽光設置工事",
    "address": "神奈川県横浜市金沢区福浦2-1-21",
    "postal_code": "",
    "survey_date": "2025/08/26",
    "weather": "",
    "surveyor": ""
  },
  "equipment": {
    "module_maker": "NextEnergy",
    "module_model": "NER108M460B-NE",
    "module_output_w": 460,
    "planned_panels": 66,
    "pv_capacity_kw": 30.36,
    "design_status": "確定"
  },
  "high_voltage": {
    "ground_type": "A",
    "single_line_diagram_note": "接地: A,D（A種とD種を併用）",
    "pcs_location": "屋外",
    "bt_space": "設置なし",
    "separation_ns_mm": 3020,
    "separation_ew_mm": 1850
  },
  "supplementary": {
    "handwritten_notes": "接地はA種とD種を併用。BT(蓄電池)設置なし。"
  }
}
```
ポイント:
- メーカー「NER108M460B-NE」型番からNextEnergy（ネクストエナジー）と特定
- module_output_w=460, planned_panels=66, pv_capacity_kw=30.36 で自己整合
  （460 × 66 / 1000 = 30.36 で完全一致）
- bt_space は「設置なし」（蓄電池を導入しない案件）
- 離隔NS=3020(カンマ区切りで「3,020」と書かれていてもmmに正規化), EW=1850

### 例3: 法人・高圧（公共施設：新座市第二老人福祉センター）
入力: 現調シート（手書き）+ 屋根伏図
読み取り結果（正解）:
```json
{
  "project": {
    "project_name": "新座市第二老人福祉センター 太陽光設置工事",
    "address": "埼玉県新座市大和田四丁目18番41号",
    "postal_code": "",
    "survey_date": "2025/05/27",
    "weather": "",
    "surveyor": ""
  },
  "equipment": {
    "module_maker": "シャープ",
    "module_model": "NU-580KG",
    "module_output_w": 580,
    "planned_panels": 40,
    "pv_capacity_kw": 23.2,
    "design_status": "確定"
  },
  "high_voltage": {
    "pcs_location": "屋内",
    "bt_space": "設置なし",
    "separation_ns_mm": 3100,
    "separation_ew_mm": 4700
  },
  "supplementary": {
    "handwritten_notes": "公共施設(老人福祉センター)。PCSは屋内設置、BT(蓄電池)設置なし。"
  }
}
```
ポイント:
- 「NU-580KG」はシャープの型番（NU-で始まる）→ メーカーは「シャープ」
- module_output_w=580, planned_panels=40, pv_capacity_kw=23.2 で自己整合
  （580 × 40 / 1000 = 23.2 で完全一致）
- PCSは屋内設置（公共施設では機械室があるため屋内が多い）
"""


# ---------------------------------------------------------------------------
# COMMERCIAL: 法人・高圧用の高精度プロンプト
# ---------------------------------------------------------------------------
COMMERCIAL_EXTRACTION_PROMPT = f"""あなたは太陽光発電設備（**法人・高圧**）の設計・施工書類を読み取る専門家です。
法人・高圧案件は通常 PV容量 10kW〜500kW、施主は法人(株式会社・有限会社・公共施設等)です。

入力には以下の書類が含まれる場合があります（複数文書が混在することが多い）:
- 現調シート（現地調査シート）: 通常2ページ構成。
  1ページ目: 案件情報・計画設備・高圧チェック項目・最終確認欄
  2ページ目（別紙）: クレーン・足場・電柱・配管/配線ルート・キュービクル・BT位置等
- 配管図: 電気配管の経路・延長距離・ケーブルラック仕様
- 単線結線図: 受変電設備の接続関係・VT/CT・接地・変圧器容量
- 屋上配置図 / 屋根伏図: パネル配置・離隔距離・パネル枚数

これらすべてから情報を統合し、後述のJSON形式で返してください。
{_COMMON_HEURISTICS}

【法人・高圧の特有チェック項目】
- ground_type: 法人・高圧では A種(避雷器・高圧機器) と D種(低圧機器) の併用が一般的。
  両方に丸が付いている場合は、ground_type に "A" を返し、
  single_line_diagram_note に "接地: A,D（A種とD種を併用）" と明記する。
- vt_available / ct_available: 高圧受電盤に通常設置される計器用変成器。
  単線結線図上に「VT」「CT」記号があれば true。
- tr_capacity: 受電変圧器の容量。「十分」or「不足」を返し、
  具体的なkVA値（300kVA, 500kVA等）が読めれば handwritten_notes に追記。
- pcs_location: パワコン設置場所「屋内」or「屋外」。
- bt_space: 蓄電池スペース「屋内」or「屋外」or「設置なし」。
  自家消費案件では「設置なし」が多い。
- 離隔距離は屋根の端からパネル端までの距離（建築基準法・消防法で規定）。
{_COMMERCIAL_FEW_SHOT_EXAMPLES}

【出力JSONスキーマ（厳守）】
```json
{{
  "project": {{
    "project_name": "案件名（敬称含む正式表記）",
    "address": "都道府県から始まる完全な所在地（郵便番号は別フィールド）",
    "postal_code": "XXX-XXXX形式の7桁郵便番号（不明なら空文字）",
    "survey_date": "調査日（YYYY/MM/DD）",
    "weather": "天気（晴れ/曇り/雨等）",
    "surveyor": "調査者名（敬称なし）"
  }},
  "equipment": {{
    "module_maker": "モジュールメーカー（候補リストから優先選択）",
    "module_model": "モジュール型式（英数字・ハイフン構成）",
    "module_output_w": 460,
    "planned_panels": 66,
    "pv_capacity_kw": 30.36,
    "design_status": "確定 / 仮 / 未定"
  }},
  "high_voltage": {{
    "building_drawing": true,
    "single_line_diagram": true,
    "single_line_diagram_note": "接地が複数選択ならここに明記",
    "ground_type": "A / C / D",
    "c_installation": "可 / 不可",
    "c_installation_note": "備考",
    "vt_available": true,
    "ct_available": true,
    "relay_space": true,
    "pcs_space": true,
    "pcs_location": "屋内 / 屋外 / null",
    "bt_space": "屋内 / 屋外 / 設置なし / null",
    "bt_backup_capacity": "",
    "tr_capacity": "十分 / 不足",
    "pre_use_self_check": true,
    "separation_ns_mm": 3020,
    "separation_ew_mm": 1850
  }},
  "supplementary": {{
    "crane_available": true,
    "scaffold_location": "",
    "scaffold_needed": false,
    "pole_number": "",
    "pole_type": "",
    "wiring_route": "確定 / 未確定",
    "cubicle_location": false,
    "bt_location": "",
    "meter_photo": "",
    "handwritten_notes": "図面から読み取った備考・配管延長・Tr容量・接地併用情報など"
  }},
  "confirmation": {{
    "surveyor_name": "",
    "surveyor_date": "",
    "design_reviewer": "",
    "design_review_date": "",
    "works_reviewer": "",
    "works_review_date": "",
    "notes": ""
  }},
  "field_confidences": {{
    "equipment.module_output_w": "high",
    "equipment.planned_panels": "high",
    "equipment.pv_capacity_kw": "high"
  }},
  "extraction_warnings": [
    "情報源（例: 現調シート1ページ目から/単線結線図から/配管図から）を明記"
  ]
}}
```

【最終指示・絶対遵守】
- 出力は **純粋なJSONオブジェクトのみ**。前置き・後書き・マークダウン・コメント・
  説明文を一切含めない。
- 説明文があると後段のパースが失敗する。**JSON以外のテキストは一切返すな**。
- 文書から読み取れない項目は空文字列・null・0・false を入れる。フィールド自体を
  省略しない。
"""


# ---------------------------------------------------------------------------
# RESIDENTIAL: 住宅・低圧用プロンプト
# ---------------------------------------------------------------------------
_RESIDENTIAL_FEW_SHOT_EXAMPLES = """
【住宅・低圧の読み取り Few-shot 事例】

### 例R1: 住宅・低圧（戸建て：施主個人）
入力: 現調シート（手書き）+ 屋根伏図 + パワコン仕様書
読み取り結果（正解イメージ）:
```json
{
  "project": {
    "project_name": "山田太郎様邸 太陽光発電設置工事",
    "address": "東京都世田谷区桜新町1-2-3",
    "postal_code": "154-0015",
    "survey_date": "2025/10/15",
    "weather": "晴れ",
    "surveyor": "高橋和浩"
  },
  "equipment": {
    "module_maker": "シャープ",
    "module_model": "NU-260AJ",
    "module_output_w": 260,
    "planned_panels": 24,
    "pv_capacity_kw": 6.24,
    "design_status": "確定"
  },
  "high_voltage": {
    "ground_type": "D",
    "pcs_location": "屋外",
    "bt_space": "設置なし",
    "separation_ns_mm": 200,
    "separation_ew_mm": 200
  },
  "supplementary": {
    "handwritten_notes": "切妻屋根。南面のみ設置。パワコン: シャープ JH-55GB3 5.5kW × 1台。回路数: 2回路。"
  }
}
```
ポイント:
- 施主は個人名（「○○様邸」表記）
- PV容量は通常 3〜10kW（< 10kW で低圧連系）
- 接地は D種のみ（家庭用低圧設備）
- 蓄電池(BT)は通常「設置なし」、ある場合は「屋外」
- パネル設置は屋根上のため離隔は200〜500mmと小さい

### 例R2: 住宅・低圧（蓄電池付き）
```json
{
  "project": {
    "project_name": "佐藤花子様邸 太陽光・蓄電池設置工事",
    "address": "神奈川県川崎市麻生区栗木台4-5-6",
    "postal_code": "215-0033",
    "survey_date": "2025/11/02"
  },
  "equipment": {
    "module_maker": "Panasonic",
    "module_model": "VBHN252WJ01",
    "module_output_w": 252,
    "planned_panels": 18,
    "pv_capacity_kw": 4.536,
    "design_status": "確定"
  },
  "high_voltage": {
    "ground_type": "D",
    "pcs_location": "屋外",
    "bt_space": "屋外",
    "bt_backup_capacity": "9.8kWh"
  },
  "supplementary": {
    "handwritten_notes": "寄棟屋根、南東/南西面に設置。蓄電池: Panasonic LJB1356 9.8kWh。ハイブリッドパワコン: VBHN-PHF55-J 5.5kW。"
  }
}
```
ポイント:
- 252W × 18枚 / 1000 = 4.536kW で自己整合
- 蓄電池あり → bt_space="屋外", bt_backup_capacity に容量を記載
"""


RESIDENTIAL_EXTRACTION_PROMPT = f"""あなたは太陽光発電設備（**住宅・低圧**）の設計・施工書類を読み取る専門家です。
住宅・低圧案件は通常 PV容量 3kW〜10kW（10kW未満で低圧連系）、施主は個人(○○様邸)です。

入力には以下の書類が含まれる場合があります:
- 現調シート（住宅向け）: 屋根形状・方位・勾配・パネル配置・設置面・離隔
- 屋根伏図: パネル配置図、回路構成、ケーブル経路
- パワコン仕様書 / 蓄電池仕様書: 機器型番・出力・容量
- 配線図: 連系方式（余剰買取/全量買取）、接地、漏電遮断器

{_COMMON_HEURISTICS}

【住宅・低圧の特有チェック項目】
- 施主名: 「○○様邸」表記が一般的（敬称「様邸」は project_name に含めて保存）。
- PV容量: < 10kW（10kW以上だと高圧連系扱いになるため、10kW超なら法人案件の可能性）。
- 接地種類: 通常 D種のみ（A種・C種は不要）。ground_type: "D"。
- pcs_location: ほぼ「屋外」（壁掛け式が多い）。屋内設置はガレージ等の場合のみ。
- bt_space: 「設置なし」or「屋外」。蓄電池併設案件が増加中。
  容量例: 4.0kWh, 6.5kWh, 9.8kWh, 12.7kWh, 16.4kWh
- 離隔距離: 屋根上設置のため、住宅は 200mm〜500mm と小さい（法人より一桁小さい）。
- パネル枚数: 通常 8〜30枚（30枚超なら法人寄り）。

【住宅特有の読み取り対象（handwritten_notes に追記）】
- 屋根形状: 切妻 / 寄棟 / 片流れ / 陸屋根 / 入母屋
- 屋根材: スレート / ガルバリウム / 瓦（和瓦・洋瓦）/ 金属屋根
- 設置面: 南面のみ / 南東+南西 / 東+西 など方位構成
- 屋根勾配: 1寸〜6寸（住宅は3〜5寸が標準）
- 回路数: 2回路 / 3回路 / 4回路（パワコンの入力回路数）
- パワコン機種: 例「シャープ JH-55GB3 5.5kW」「Panasonic VBPC255GS3 5.5kW」
- 連系方式: 余剰買取（住宅FIT） / 全量買取 / 自家消費

【住宅向けパワコン典型機種（メーカー候補拡張）】
- シャープ: JH-55GB3, JH-55JF4, JH-44GB3
- Panasonic: VBPC255GS3, VBHN-PHF55-J（ハイブリッド）
- オムロン: KP55M2-J4, KP59R-J4
- ニチコン: ESS-T3S1, ESS-U2X1（蓄電池）
- 田淵電機: EIBS7（ハイブリッド）

{_RESIDENTIAL_FEW_SHOT_EXAMPLES}

【出力JSONスキーマ（厳守・法人版と同形式）】
```json
{{
  "project": {{
    "project_name": "○○様邸 太陽光発電設置工事 等",
    "address": "都道府県から始まる完全な所在地",
    "postal_code": "XXX-XXXX形式の7桁郵便番号",
    "survey_date": "YYYY/MM/DD",
    "weather": "天気",
    "surveyor": "調査者名（敬称なし）"
  }},
  "equipment": {{
    "module_maker": "モジュールメーカー",
    "module_model": "モジュール型式",
    "module_output_w": 260,
    "planned_panels": 24,
    "pv_capacity_kw": 6.24,
    "design_status": "確定 / 仮 / 未定"
  }},
  "high_voltage": {{
    "building_drawing": true,
    "single_line_diagram": false,
    "single_line_diagram_note": "",
    "ground_type": "D",
    "c_installation": "可 / 不可",
    "c_installation_note": "",
    "vt_available": false,
    "ct_available": false,
    "relay_space": false,
    "pcs_space": true,
    "pcs_location": "屋内 / 屋外 / null",
    "bt_space": "屋内 / 屋外 / 設置なし / null",
    "bt_backup_capacity": "9.8kWh 等",
    "tr_capacity": "",
    "pre_use_self_check": false,
    "separation_ns_mm": 200,
    "separation_ew_mm": 200
  }},
  "supplementary": {{
    "crane_available": false,
    "scaffold_location": "",
    "scaffold_needed": true,
    "pole_number": "",
    "pole_type": "",
    "wiring_route": "確定 / 未確定",
    "cubicle_location": false,
    "bt_location": "",
    "meter_photo": "",
    "handwritten_notes": "屋根形状・屋根材・設置面・勾配・回路数・パワコン機種・連系方式など住宅特有情報"
  }},
  "confirmation": {{
    "surveyor_name": "",
    "surveyor_date": "",
    "design_reviewer": "",
    "design_review_date": "",
    "works_reviewer": "",
    "works_review_date": "",
    "notes": ""
  }},
  "field_confidences": {{
    "equipment.module_output_w": "high",
    "equipment.planned_panels": "high",
    "equipment.pv_capacity_kw": "high"
  }},
  "extraction_warnings": [
    "情報源を明記（例: 屋根伏図から/パワコン仕様書から）"
  ]
}}
```

【最終指示・絶対遵守】
- 出力は **純粋なJSONオブジェクトのみ**。前置き・後書き・マークダウン・コメント・
  説明文を一切含めない。
- 説明文があると後段のパースが失敗する。**JSON以外のテキストは一切返すな**。
- PV容量が10kW以上の場合は、法人案件の可能性があるため
  extraction_warnings に「PV容量が10kW超のため法人・高圧案件の可能性あり」と追記する。
- 文書から読み取れない項目は空文字列・null・0・false を入れる。
"""


# ---------------------------------------------------------------------------
# CLASSIFICATION: 文書を commercial / residential / unknown に分類するプロンプト
# ---------------------------------------------------------------------------
CLASSIFICATION_PROMPT = """あなたは太陽光発電案件の書類を分類する専門家です。

入力された画像（PDFから変換）が、以下のいずれの案件タイプに属するかを判定してください:

1. **commercial（法人・高圧）**:
   - 施主が法人（株式会社・有限会社・公共施設・自治体・学校・工場・物流倉庫など）
   - PV容量が 10kW以上（典型: 30〜500kW）
   - 受変電設備（キュービクル・トランス・VT/CT）が登場する
   - 「自家消費」「高圧連系」「キュービクル」「変圧器」「PCS」などのキーワード
   - 単線結線図・配管図・屋根伏図など複数の専門図面が含まれる
   - 接地種類が A種・C種・D種のいずれか/複数

2. **residential（住宅・低圧）**:
   - 施主が個人（「○○様邸」「○○家」表記）
   - PV容量が 10kW未満（典型: 3〜9kW）
   - パネル枚数が 8〜30枚程度
   - 屋根形状（切妻/寄棟/片流れ）への設置
   - 「余剰買取」「FIT」「住宅用パワコン」などのキーワード
   - 接地種類は D種のみ
   - 簡素な現調シート + 屋根伏図 + パワコン仕様書の構成

3. **unknown**:
   - 上記どちらにも明確に該当しない、または判別不可能

【判定の優先順位】
1. 施主名/案件名（「株式会社」を含む → commercial、「○○様邸」→ residential）
2. PV容量 / パネル枚数（10kW・30枚を境に commercial/residential を分ける）
3. 受変電設備の有無（VT/CT/キュービクルがあれば commercial）
4. 接地種類（A種かC種が登場 → commercial、D種のみ → residential 寄り）
5. 文書種類（単線結線図・配管図あり → commercial 寄り）

【判定材料が両方の特徴を持つ場合】
- commercialの特徴を1つでも明確に持てば commercial と判定する
  （例: 個人邸の表記でもキュービクルが図示されていれば commercial）
- 判断が割れる場合は安全側に倒し commercial を選ぶ
  （commercialプロンプトの方が情報量が多く対応範囲が広いため）

【出力フォーマット（厳守）】
以下のJSONのみを返してください。説明文・前置き・後書きは一切不要です。

```json
{
  "category": "commercial",
  "confidence": "high",
  "reasoning": "判定根拠を1〜2文で簡潔に記載（例: 案件名に株式会社を含み、単線結線図とVT/CT記載があるため法人・高圧と判定）",
  "indicators": {
    "owner_type": "法人 / 個人 / 不明",
    "estimated_capacity_kw": 150.4,
    "panel_count": 376,
    "has_high_voltage_equipment": true,
    "ground_type_detected": "A,D"
  }
}
```

- category: "commercial" / "residential" / "unknown" のいずれか
- confidence: "high" / "medium" / "low"
- reasoning: 判定の決め手を簡潔に
- indicators: 判定材料の数値・特徴（不明な項目は null か空文字）

【最終指示・絶対遵守】
**JSON以外のテキストは一切返すな**。純粋なJSONオブジェクトのみを返すこと。
"""
