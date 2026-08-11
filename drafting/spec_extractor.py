"""現調資料 → DraftingSpec の Vision 抽出（簡易製図AI）

手書きスケッチ／航空写真＋赤ペン寸法／建築図面／現地写真（PDF・PNG・JPG）を
Claude Vision で読み取り、drafting/models.py の DraftingSpec を生成する。

設計方針:
- 入力は複数ファイル混在可（PDF は extraction.pdf_reader.pdf_to_images で画像化、
  PNG/JPG は読み込んで base64 化）。全画像を 1 リクエストの content に並べて
  JSON 抽出を依頼する。画像枚数・合計サイズが大きい場合は間引き／ダウンスケールする。
- few-shot として drafting/sample_specs.py の正解 spec を spec_to_dict した JSON を
  プロンプトに埋め込む（kurihara_layout = 住宅単面・横置き、tok_string = 法人複数面・系統あり）。
- 出力は DraftingSpec の dict 構造に厳密準拠した JSON。spec_from_dict が受理できる形にして
  復元する。読めない寸法は推定せず 0/null とし warnings に記録する。
- API キー無し／API 失敗時は例外を投げず、warnings に理由を入れた最小 spec を返す
  （呼び出し側で確認フォーム編集する前提のフォールバック）。
- confidence（主要フィールド→ high/medium/low）と warnings（人間が確認すべき点）を埋める。

公開関数:
- extract_drafting_spec(file_paths, *, drawing_type="layout", hint="") -> DraftingSpec
- extract_drafting_spec_from_images(images, *, drawing_type="layout", hint="") -> DraftingSpec
"""

from __future__ import annotations

import base64
import io
import json
import logging
import os
import re
import time
from typing import Optional

from drafting.models import (
    DraftingSpec,
    DrawingType,
    RoofType,
    Orientation,
    MountType,
    spec_to_dict,
    spec_from_dict,
    default_spec,
)
from drafting.sample_specs import get_golden

logger = logging.getLogger(__name__)

# ---- 動作パラメータ ----
MAX_TOTAL_IMAGES = 12          # 1 リクエストに載せる画像枚数の上限（多すぎるとAPI制限/精度低下）
MAX_IMAGE_BYTES = 4_500_000    # 画像1枚の base64 前バイト上限（pdf_reader と同じ閾値）
MAX_IMAGE_DIM_PX = 4000        # 画像1枚の最大ピクセル寸法（Anthropic上限8000pxに対し安全側）
MAX_REQUEST_BYTES = 28_000_000 # 1 リクエストの画像合計バイト上限（API上限32MBに対し安全側）
DEFAULT_DPI = 200              # PDF → 画像の解像度
MAX_RETRIES = 3                # API リトライ回数
RETRY_DELAY_SEC = 2            # リトライ初回待機秒（指数バックオフ）

# 画像拡張子 → media_type
_IMAGE_MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}

# 受理する屋根種別・向き・架台（プロンプト誘導＆検証用）
_VALID_ROOF_TYPES = set(RoofType.ALL)
_VALID_ORIENTATIONS = set(Orientation.ALL)
_VALID_MOUNT_TYPES = set(MountType.ALL)
_VALID_DRAWING_TYPES = set(DrawingType.ALL)


# =============================================================
# 入力ファイル → 画像 dict 群
# =============================================================

def _guess_media_type(path: str) -> str:
    """拡張子から画像 media_type を推定する。"""
    ext = os.path.splitext(path)[1].lower()
    return _IMAGE_MEDIA_TYPES.get(ext, "image/png")


def _downscale_image_bytes(
    img_bytes: bytes,
    max_bytes: int = MAX_IMAGE_BYTES,
    max_dim_px: int = MAX_IMAGE_DIM_PX,
) -> tuple[bytes, str]:
    """画像が大きすぎる場合に JPEG 圧縮／リサイズして上限内に収める。

    バイト数（max_bytes）だけでなく、ピクセル寸法（max_dim_px）も上限とする。
    横長の線画スキャン（屋根伏図・航空写真）は数千〜1万px幅でもPNGが小さく、
    バイト数では検知できないまま Anthropic の寸法上限(8000px)を超えて
    "Could not process image"(400) を起こすため、寸法でも判定する。

    Pillow が無い／変換に失敗した場合は元のバイト列をそのまま返す（副作用なし）。

    Returns:
        (バイト列, media_type)
    """
    try:
        from PIL import Image  # 遅延 import（無くてもフォールバックする）
    except Exception:
        if len(img_bytes) > max_bytes:
            logger.warning("Pillow が無いため画像ダウンスケールをスキップします（元画像を使用）。")
        return img_bytes, ""

    # 寸法を確認（バイト・寸法とも上限内なら原本のまま）
    try:
        with Image.open(io.BytesIO(img_bytes)) as probe:
            w, h = probe.size
    except Exception:
        # 開けない場合はバイト数のみで判断（従来挙動）
        return img_bytes, ""
    if len(img_bytes) <= max_bytes and max(w, h) <= max_dim_px:
        return img_bytes, ""  # 変換不要（呼び出し側で元の media_type を使う）

    try:
        pil_img = Image.open(io.BytesIO(img_bytes))
        if pil_img.mode not in ("RGB", "L"):
            pil_img = pil_img.convert("RGB")
        # 段階的にリサイズ＋JPEG品質を下げる
        for max_dim in (2000, 1600, 1200, 1000):
            w, h = pil_img.size
            if max(w, h) > max_dim:
                scale = max_dim / max(w, h)
                resized = pil_img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
            else:
                resized = pil_img
            for quality in (85, 75, 65, 55):
                buf = io.BytesIO()
                resized.convert("RGB").save(buf, format="JPEG", quality=quality)
                jpeg = buf.getvalue()
                if len(jpeg) <= max_bytes:
                    return jpeg, "image/jpeg"
        # 最終手段
        buf = io.BytesIO()
        pil_img.convert("RGB").save(buf, format="JPEG", quality=40)
        return buf.getvalue(), "image/jpeg"
    except Exception as e:
        logger.warning(f"画像ダウンスケールに失敗（元画像で続行）: {e}")
        return img_bytes, ""


def _load_file_as_images(path: str) -> list[dict]:
    """1 ファイルを画像 dict のリストに変換する。

    - PDF: extraction.pdf_reader.pdf_to_images で全ページを画像化。
    - 画像（PNG/JPG/...）: 読み込んで base64 化（大きすぎる場合はダウンスケール）。

    Returns:
        list of {"page": int, "image_base64": str, "image_bytes": bytes, "media_type": str}
        変換できない場合は空リスト（warnings は呼び出し側で集計）。
    """
    if not path or not os.path.isfile(path):
        raise FileNotFoundError(f"ファイルが見つかりません: {path}")

    ext = os.path.splitext(path)[1].lower()

    if ext == ".pdf":
        # PDF は既存ユーティリティに委譲（回転補正・コントラスト強調・サイズ圧縮込み）
        from extraction.pdf_reader import pdf_to_images
        return pdf_to_images(path, dpi=DEFAULT_DPI)

    if ext in _IMAGE_MEDIA_TYPES:
        with open(path, "rb") as f:
            raw = f.read()
        media_type = _guess_media_type(path)
        new_bytes, new_media = _downscale_image_bytes(raw)
        if new_media:
            raw = new_bytes
            media_type = new_media
        return [{
            "page": 1,
            "image_base64": base64.standard_b64encode(raw).decode("utf-8"),
            "image_bytes": raw,
            "media_type": media_type,
        }]

    raise ValueError(f"未対応の拡張子です（PDF/PNG/JPG のみ対応）: {ext}")


def _collect_images(file_paths: list[str]) -> tuple[list[dict], list[str]]:
    """複数ファイルを画像 dict 群へ展開する。

    Returns:
        (images, warnings)  画像化に失敗したファイルは warnings に記録してスキップ。
    """
    images: list[dict] = []
    warnings: list[str] = []
    for path in file_paths or []:
        try:
            imgs = _load_file_as_images(path)
            if not imgs:
                warnings.append(f"画像を取得できませんでした（空の結果）: {os.path.basename(path)}")
                continue
            images.extend(imgs)
        except FileNotFoundError as e:
            warnings.append(str(e))
        except Exception as e:
            warnings.append(f"ファイルの画像化に失敗しました（{os.path.basename(path)}）: {e}")
            logger.warning(f"画像化失敗: {path}: {e}")
    return images, warnings


# =============================================================
# プロンプト生成
# =============================================================

def _golden_example_json(name: str) -> str:
    """ゴールデン spec を「正解出力例」用の JSON 文字列にする（座標 panels は除く）。

    panels は抽出フェーズでは空（layout_engine が後で埋める）ため、軽量化のため落とす。
    """
    d = spec_to_dict(get_golden(name))
    for face in d.get("roof_faces", []):
        face["panels"] = []  # 抽出段階では座標を持たない
    return json.dumps(d, ensure_ascii=False, indent=2)


def _learned_examples_block() -> str:
    """学習センターで承認された実案件の正解 spec を few-shot 追記ブロックにする。

    learning ストアが無い/壊れている場合は空文字を返し、従来プロンプトのまま動く
    （学習系の失敗で抽出フローを止めない）。プロンプト肥大防止のため2件まで。
    """
    try:
        from learning.apply_drawing import learned_golden_examples
        blocks = []
        for i, ex in enumerate(learned_golden_examples(2), start=1):
            spec_d = ex.get("spec") if isinstance(ex, dict) else None
            if not isinstance(spec_d, dict):
                continue
            # deepcopy 相当（元の学習ストア payload を汚さない）+ 座標 panels を落として軽量化
            d = json.loads(json.dumps(spec_d, ensure_ascii=False))
            for face in d.get("roof_faces", []) or []:
                if isinstance(face, dict):
                    face["panels"] = []
            name = ex.get("name") or f"学習例{i}"
            blocks.append(
                f"\n【実案件の学習済みお手本{i}（{name}。過去に人が確認した正解）】\n"
                "```json\n" + json.dumps(d, ensure_ascii=False, indent=2) + "\n```\n"
            )
        return "".join(blocks)
    except Exception as e:
        logger.debug(f"学習済みお手本の注入をスキップ: {e}")
        return ""


def build_system_prompt() -> str:
    """システムプロンプト（抽出専門家のロール定義）を生成する。"""
    return (
        "あなたは太陽光発電設備の現地調査資料（現調資料）から、製図に必要な仕様を読み取る専門家です。\n"
        "入力画像は次のいずれか、または複数の組み合わせです:\n"
        "- 手書きスケッチ（屋根の形・寸法を赤ペン等で書き込んだもの）\n"
        "- 航空写真／衛星写真に寸法や枚数を赤ペンで書き込んだもの\n"
        "- 建築図面（屋根伏図・平面図・立面図）\n"
        "- 現地写真\n\n"
        "手書きの日本語・数字を文脈から正確に読み取ってください。\n"
        "寸法はすべて mm（ミリメートル）に統一して出力してください"
        "（「8.85m」「885cm」のような表記は 8850 に換算）。\n"
        "読み取れない寸法・項目は推定せず、0 または null とし、warnings に必ず記載してください。\n"
        "出力は指定された JSON 構造のみとし、説明文・前置き・マークダウンは一切含めないでください。"
    )


def build_user_prompt(drawing_type: str = DrawingType.LAYOUT, hint: str = "") -> str:
    """ユーザープロンプト（抽出指示＋スキーマ＋few-shot）を生成する。

    Args:
        drawing_type: 図面種別（layout/string/equipment）。出力 JSON の drawing_type に反映。
        hint: 呼び出し側からの補足ヒント（例: 施主名・既知のメーカー等）。空なら無視。

    Returns:
        ユーザープロンプト文字列。
    """
    dtype = drawing_type if drawing_type in _VALID_DRAWING_TYPES else DrawingType.LAYOUT
    dtype_label = DrawingType.LABEL.get(dtype, dtype)

    roof_type_lines = "\n".join(f'    - "{k}" = {v}' for k, v in RoofType.LABEL.items())
    orient_lines = "\n".join(f'    - "{k}" = {v}' for k, v in Orientation.LABEL.items())
    mount_lines = "\n".join(f'    - "{m}"' for m in MountType.ALL)

    example_kurihara = _golden_example_json("kurihara_layout")
    example_tok = _golden_example_json("tok_string")
    learned_block = _learned_examples_block()  # 学習済みお手本（無ければ空）

    hint_block = ""
    if hint and hint.strip():
        hint_block = (
            "\n【追加ヒント（呼び出し側からの補足。矛盾する場合は画像を優先）】\n"
            f"{hint.strip()}\n"
        )

    return f"""次の画像群は太陽光発電の現調資料です。これらから製図用の仕様を抽出してください。
作成する図面種別は「{dtype_label}」（drawing_type="{dtype}"）です。

【抽出してほしい情報】
1. 施主名 / 工事名（customer_name, title.project_name）
2. モジュール（パネル）: メーカー・型番・1枚出力W・長辺mm・短辺mm（panel）
3. 屋根面ごとに:
   - 名称（複数面なら「面1」「面2」…）
   - 屋根種別（瓦/折板/陸屋根/スレート）。「陸屋根」「屋上」「RC屋上」→ rikuyane、
     「折板」「金属屋根」→ setsuban、傾斜屋根は屋根材の記載から判定
     （瓦→kawara、スレート/コロニアル/カラーベスト→slate）
   - 形状（矩形 rectangle / 多角形 polygon）と寸法 幅mm・奥行mm（多角形なら頂点列 polygon_mm）
   - その面の設置枚数（target_panel_count。資料に枚数の記載が無ければ null —
     その場合は屋根面に収まる最大枚数を自動配置する。推定枚数を入れないこと。
     「16枚」のような枚数ラベルが屋根の区画ごとに書かれている場合は、
     区画ごとに別の roof_face に分けて出力する〔1面=1枚数ラベル〕）
   - 面の位置（origin_x_mm / origin_y_mm）: 複数面がある場合、資料上の面同士の
     位置関係（どちらが右/下か・面の間の距離）を図面上でも再現したいので、
     読み取れる範囲で設定する。位置関係が読み取れない場合は面同士が重ならない
     よう（例: 面2は面1の右に「面1の幅＋2000mm」）ずらして並べ、warnings に
     「面の相対位置は仮配置」と記載する
   - パネルの向き（縦置き portrait / 横置き landscape）
   - 屋根端・軒・ケラバからの離隔の記載（例「離隔500」「端部より500逃げ」）があれば
     margin_mm へ。アレイ間・段間の間隔（長辺側隙間）の記載があれば
     panel.gap_long_mm（行間・段と段の間）へ
4. 総枚数・設置容量: 「68枚」「設置容量:34.68kW(68枚)」等の記載があれば
   total_panels / total_kw に転記する。各面 target_panel_count の合計が総枚数と
   一致するか、また kW ÷ モジュール出力W ≒ 枚数になるかを検算し、一致しない
   場合はどちらを採用したかと差分を warnings に記載する
5. 架台種別（mount_type）
6. PCS（パワコン）の型番・台数（pcs_model, pcs_count）
7. ストリング系統（◯直×◯並。例「12直×5並」）→ strings（PCSごと）
8. 図番・作成日・縮尺など（title）

【寸法・向きの単位ルール】
- すべて mm。m/cm 表記は mm に換算。
- 屋根面ローカル座標は 左上原点・x=右・y=下。多角形 polygon_mm は閉じない頂点列 [[x,y],...]
  （頂点は最小x・最小yが 0 になるよう平行移動して 0 起点で出力する）。
- 寸法線は、その線がどの辺・区間を指すか（矢印・引出線の範囲）を確認してから採用する。
  部分寸法の列（例: 2500+5241+6559）と全体寸法（14300）が両方ある場合は合計の一致を
  検算し、合わない読み取りは採用せず warnings に記載する。折板屋根で同じスパン寸法
  （例: 3816）が繰り返し並ぶ場合、屋根幅はその繰り返しの合計。
- 折板屋根は通常パネル縦置き（portrait）・縦ハッチング、瓦/スレートは横ハッチングが多い。
- 読めない寸法はキーを省略するか null にする（0 と書くのは「0mm」と明記されている
  場合のみ）。推定値を入れず warnings に「面Xの奥行が判読不能」等を記載。

【屋根種別 roof_type の取りうる値】
{roof_type_lines}

【向き orientation の取りうる値】
{orient_lines}

【架台種別 mount_type の取りうる値】
{mount_lines}

【出力フォーマット（必須）】
drafting/models.py の DraftingSpec の dict 構造に厳密準拠した JSON のみを返してください。
- 数値は数値型で（文字列にしない）。寸法 mm は整数または小数。
- confidence は主要フィールド名→"high"/"medium"/"low" の辞書。手書きで曖昧な値は "low"。
- warnings は人間が確認すべき点（手書き寸法の読み取り曖昧箇所、判読不能項目など）の文字列配列。
- drawing_type は必ず "{dtype}" にしてください。
- gap_long_mm / gap_short_mm / margin_mm / walkway_mm / パネル寸法（long_mm,
  short_mm）は、資料に明記が無い場合は**キー自体を出力しない**でください
  （キーが無ければ屋根種別ごとの標準値が使われます。0 を書くのは「0mm」と
  明記されている場合のみ。不明を 0 にすると隙間ゼロ・寸法ゼロとして扱われ、
  配置計算が壊れます）。
- その他の不明な項目は、文字列は ""、配列は []、数値は null にしてください。
{hint_block}
【正解出力例1（住宅・瓦・配置図・横置き10枚。単一屋根面・系統なし）】
```json
{example_kurihara}
```

【正解出力例2（法人・ストリングス図・複数屋根面・PCS3台・系統表あり）】
```json
{example_tok}
```
{learned_block}
上記の例と同じキー構造で、今回の画像から読み取った JSON を返してください。
※例では confidence / warnings を簡略化していますが、実際の出力では手書きの
読み取りに少しでも迷った項目（寸法・枚数・角度・施主名の漢字・型番など）を
必ず confidence（"low"/"medium"）と warnings に挙げてください。
panels（座標）は空配列 [] のままで構いません（配置計算は別工程で行います）。
JSON 以外のテキストは一切出力しないでください。"""


# =============================================================
# Claude API 呼び出し（survey_extractor のパターンを踏襲）
# =============================================================

def _build_content(images: list[dict], user_prompt: str) -> list[dict]:
    """画像群＋プロンプトを Claude messages の content に組み立てる。"""
    content: list[dict] = []
    for page in images:
        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": page.get("media_type", "image/png"),
                "data": page["image_base64"],
            },
        })
    content.append({"type": "text", "text": user_prompt})
    return content


def _call_claude_api(content: list[dict], system_prompt: str, attempt: int,
                     temperature: float = 0.0) -> dict:
    """Claude Vision API を呼び出して JSON dict を返す。

    前置きやコードフェンス混じりの応答は survey_extractor の _extract_json で
    除去する（Sonnet 4.6 以降は assistant プリフィルが 400 になるため使用しない）。

    Raises:
        各種 anthropic 例外 / json.JSONDecodeError / ValueError（呼び出し側でリトライ判定）
    """
    import anthropic
    from config import get_api_key
    # JSON抽出・Visionモデル切替（CLAUDE_VISION_MODEL + Fable差分吸収 +
    # フォールバック）は survey_extractor の実装を流用（重複実装を避ける）
    from extraction.survey_extractor import (
        _extract_json, _create_vision_message, _first_text_block)

    client = anthropic.Anthropic(api_key=get_api_key())
    response = _create_vision_message(
        client, content, temperature, system=system_prompt)
    response_text = _first_text_block(response)
    logger.info(f"Vision応答（試行{attempt}）: {response_text[:200]}...")
    json_str = _extract_json(response_text)
    return json.loads(json_str)


# =============================================================
# パース・正規化・信頼度補完
# =============================================================

def _normalize_parsed(parsed: dict, drawing_type: str) -> tuple[dict, list[str], dict]:
    """API が返した dict を spec_from_dict が受理しやすい形に正規化する。

    - drawing_type を強制反映。
    - roof_type / orientation / mount_type の値域を検証し、不正なら既定値＋warning。
    - 数値文字列（"8850" 等）は数値へ寛容変換。
    - 各面の panels は空配列に統一（座標は別工程）。

    Returns:
        (正規化後 dict, 追加 warnings, 追加 confidence)
    """
    warnings: list[str] = []
    confidence: dict = {}
    if not isinstance(parsed, dict):
        return {}, ["AIの応答が辞書形式ではありませんでした。"], {}

    d = dict(parsed)
    d["drawing_type"] = drawing_type if drawing_type in _VALID_DRAWING_TYPES else DrawingType.LAYOUT

    # mount_type の検証
    mount = d.get("mount_type")
    if mount and mount not in _VALID_MOUNT_TYPES:
        warnings.append(f"架台種別「{mount}」が想定値に一致しません。確認してください。")
        confidence["mount_type"] = "low"

    # panel の数値寛容変換
    panel = d.get("panel")
    if isinstance(panel, dict):
        for k in ("output_w", "long_mm", "short_mm", "gap_long_mm", "gap_short_mm",
                  "walkway_mm"):
            if k in panel:
                panel[k] = _coerce_number(panel[k])
        # 出力Wの補完: output_w が読み取れないと図面の設置容量が
        # 「0.000kW」になる（2026-08-10 顧客提供図面①②で実発生）。
        # 型式文字列に含まれるW数（例: XLN120G-510X → 510）から推定する。
        if _coerce_number(panel.get("output_w", 0) or 0) <= 0:
            guessed = _wattage_from_model(str(panel.get("model", "") or ""))
            if guessed:
                panel["output_w"] = float(guessed)
                warnings.append(
                    f"モジュール出力Wが読み取れなかったため、型式"
                    f"「{panel.get('model')}」から {guessed}W と推定しました。"
                    "確認してください。")
                confidence["panel.output_w"] = "low"
        # パネル寸法の補完: 寸法0のまま配置に進むと0枚配置になり、旧実装では
        # 描画側が既定寸法のダミーグリッドを描く事故だった（2026-08-11 分析）。
        # 型式マスター（knowledge/panel_dimensions.yaml）/W数典型値から補完する。
        watt = _coerce_number(panel.get("output_w", 0) or 0)
        long_v = _coerce_number(panel.get("long_mm", 0) or 0)
        short_v = _coerce_number(panel.get("short_mm", 0) or 0)
        if (long_v <= 0 or short_v <= 0) and watt > 0:
            try:
                from roof.panel_layout import panel_dimensions_from_module
                long_m, short_m = panel_dimensions_from_module(
                    str(panel.get("maker", "") or ""),
                    str(panel.get("model", "") or ""), watt)
            except Exception:
                long_m = short_m = 0
            if long_m and short_m:
                panel["long_mm"] = float(round(long_m * 1000))
                panel["short_mm"] = float(round(short_m * 1000))
                warnings.append(
                    f"モジュール寸法が読み取れなかったため、型式・出力Wから "
                    f"{int(panel['long_mm'])}×{int(panel['short_mm'])}mm と"
                    "推定しました。確認してください。")
                confidence["panel.dimensions"] = "low"

    # ルート直下の数値（pcs_count / total_panels 等）も寛容変換し、
    # spec_from_dict の int() が "3台" 等で落ちて全抽出が失われるのを防ぐ。
    for k in ("pcs_count", "total_panels"):
        if k in d:
            d[k] = int(_coerce_number(d[k]))
    if "total_kw" in d:
        d["total_kw"] = _coerce_number(d["total_kw"])

    # 屋根面の検証・正規化
    faces = d.get("roof_faces")
    if isinstance(faces, list):
        for idx, face in enumerate(faces, start=1):
            if not isinstance(face, dict):
                continue
            name = face.get("name") or f"面{idx}"
            # roof_type
            rt = face.get("roof_type")
            if rt and rt not in _VALID_ROOF_TYPES:
                warnings.append(f"{name}: 屋根種別「{rt}」が想定値外です。瓦屋根として扱います。")
                face["roof_type"] = RoofType.KAWARA
                confidence[f"roof_faces.{idx}.roof_type"] = "low"
            # orientation
            ori = face.get("orientation")
            if ori and ori not in _VALID_ORIENTATIONS:
                warnings.append(f"{name}: 向き「{ori}」が想定値外です。自動配置にします。")
                face["orientation"] = Orientation.AUTO
                confidence[f"roof_faces.{idx}.orientation"] = "low"
            # 数値寛容変換
            for k in ("width_mm", "depth_mm", "origin_x_mm", "origin_y_mm", "margin_mm"):
                if k in face:
                    face[k] = _coerce_number(face[k])
            if "target_panel_count" in face and face["target_panel_count"] is not None:
                face["target_panel_count"] = int(_coerce_number(face["target_panel_count"]))
            # 寸法欠損チェック（矩形で幅/奥行が両方0 → low + warning）
            shape = face.get("shape", "rectangle")
            if shape == "rectangle":
                w = _coerce_number(face.get("width_mm", 0))
                h = _coerce_number(face.get("depth_mm", 0))
                if w <= 0 or h <= 0:
                    warnings.append(f"{name}: 屋根寸法（幅/奥行）が読み取れていません。確認してください。")
                    confidence[f"roof_faces.{idx}.dimensions"] = "low"
            elif shape == "polygon":
                poly = face.get("polygon_mm")
                if not poly or not isinstance(poly, list) or len(poly) < 3:
                    warnings.append(f"{name}: 多角形の頂点が不足しています。確認してください。")
                    confidence[f"roof_faces.{idx}.polygon"] = "low"
                else:
                    # 頂点座標も数値へ寛容変換（"8850" 等の文字列のまま配置計算に渡すと落ちる）
                    coerced = []
                    for pt in poly:
                        if isinstance(pt, (list, tuple)) and len(pt) >= 2:
                            coerced.append([_coerce_number(pt[0]), _coerce_number(pt[1])])
                    face["polygon_mm"] = coerced if len(coerced) >= 3 else poly
            # panels は座標を持たない（別工程）
            face["panels"] = []
    elif faces is not None:
        warnings.append("roof_faces が配列ではありませんでした。屋根面情報を確認してください。")
        d["roof_faces"] = []

    # --- 枚数・容量の検算（30/68枚事故の再発防止。2026-08-11 分析） ---
    # 面ごと枚数の合計 vs 総枚数、kW÷W vs 総枚数 の不一致は配置枚数事故の
    # 前兆のため、warning で必ず表面化させる。
    total_panels = int(_coerce_number(d.get("total_panels", 0) or 0))
    target_sum = sum(
        int(_coerce_number(f.get("target_panel_count") or 0))
        for f in (d.get("roof_faces") or []) if isinstance(f, dict))
    if total_panels > 0 and target_sum > 0 and total_panels != target_sum:
        warnings.append(
            f"面ごとの枚数合計（{target_sum}枚）と総枚数（{total_panels}枚）が"
            "一致しません。屋根面の分け方・枚数ラベルを確認してください。")
        confidence["total_panels"] = "low"
    total_kw = _coerce_number(d.get("total_kw", 0) or 0)
    watt_chk = 0.0
    if isinstance(d.get("panel"), dict):
        watt_chk = _coerce_number(d["panel"].get("output_w", 0) or 0)
    if total_kw > 0 and watt_chk > 0 and total_panels > 0:
        kw_calc = total_panels * watt_chk / 1000.0
        if abs(kw_calc - total_kw) > max(0.02 * total_kw, 0.01):
            warnings.append(
                f"設置容量の記載（{total_kw}kW）と 枚数×出力W の計算値"
                f"（{kw_calc:.3f}kW）が一致しません。枚数・型式を確認してください。")
            confidence["total_kw"] = "low"

    return d, warnings, confidence


def _wattage_from_model(model: str) -> int:
    """型式文字列からモジュール出力W数を推定する（例: XLN120G-510X → 510）。

    3〜4桁の数値のうち実在するモジュール出力のレンジ（300〜750W）に
    収まるものを候補とし、複数あれば最後のものを採用する
    （NER108M465B → 465、LR7-72HVH-645M → 645）。該当なしは 0。
    直後に V が続く数値は電圧表記（例: 600V）とみなして除外する。
    """
    if not model:
        return 0
    candidates = [int(s) for s in re.findall(r"\d{3,4}(?![0-9Vv])", model)]
    candidates = [c for c in candidates if 300 <= c <= 750]
    return candidates[-1] if candidates else 0


def _coerce_number(val):
    """数値文字列（"8850", "8.85m" 等）を寛容に数値化する。失敗時は元値/0。"""
    if isinstance(val, (int, float)):
        return val
    if val is None:
        return 0
    if isinstance(val, str):
        import re
        s = val.strip()
        # m / cm 換算（末尾単位のみ簡易対応。単一小数のみ許容）
        m = re.match(r"^(\d+(?:\.\d+)?)\s*(m|cm|mm)?$", s, re.IGNORECASE)
        if m:
            try:
                num = float(m.group(1))
            except ValueError:
                return 0
            unit = (m.group(2) or "").lower()
            if unit == "m":
                return num * 1000
            if unit == "cm":
                return num * 10
            return num
        # それ以外（"8.85.0m" 等の誤読・記号混じり）は数字とドットのみ残して再試行。
        # 複数ドットなら最初の小数のみ採用し、失敗しても 0 を返す（他フィールドを保護）。
        cleaned = re.sub(r"[^\d.]", "", s)
        mm2 = re.match(r"^(\d+(?:\.\d+)?)", cleaned)
        if mm2:
            try:
                return float(mm2.group(1))
            except ValueError:
                return 0
        return 0
    return 0


def _auto_confidence(spec: DraftingSpec) -> dict:
    """主要フィールドの確信度を自動補完する（API が confidence を返さなかった場合）。"""
    conf: dict = {}
    if not spec.customer_name:
        conf["customer_name"] = "low"
    if not spec.panel.maker:
        conf["panel.maker"] = "low"
    if not spec.panel.model:
        conf["panel.model"] = "low"
    if spec.panel.output_w <= 0:
        conf["panel.output_w"] = "low"
    if spec.panel.long_mm <= 0 or spec.panel.short_mm <= 0:
        # 寸法不明のままだと配置不能（place_panels がスキップして警告する）
        conf["panel.dimensions"] = "low"
    if not spec.roof_faces:
        conf["roof_faces"] = "low"
    return conf


# =============================================================
# フォールバック spec
# =============================================================

def _fallback_spec(drawing_type: str, reason: str, extra_warnings: Optional[list] = None) -> DraftingSpec:
    """API 不可／失敗時に返す最小 spec（例外を投げない）。

    default_spec をベースに customer_name を空にし、warnings に理由を入れる。
    呼び出し側で確認フォーム編集する前提。
    """
    spec = default_spec()
    spec.customer_name = ""
    spec.drawing_type = drawing_type if drawing_type in _VALID_DRAWING_TYPES else DrawingType.LAYOUT
    msgs = [reason]
    if extra_warnings:
        msgs.extend(extra_warnings)
    spec.warnings = msgs
    spec.confidence = {"_extraction": "low"}
    return spec


# =============================================================
# 公開関数
# =============================================================

def extract_drafting_spec_from_images(
    images: list[dict],
    *,
    drawing_type: str = DrawingType.LAYOUT,
    hint: str = "",
) -> DraftingSpec:
    """画像 dict 群（pdf_reader 互換）から DraftingSpec を抽出する。

    Args:
        images: list of {"image_base64", "media_type", ...}（pdf_to_images の出力互換）。
        drawing_type: 図面種別（layout/string/equipment）。
        hint: 補足ヒント文字列。

    Returns:
        DraftingSpec。API キー無し／失敗時は warnings 入りの最小 spec（例外は投げない）。
    """
    drawing_type = drawing_type if drawing_type in _VALID_DRAWING_TYPES else DrawingType.LAYOUT

    if not images:
        return _fallback_spec(drawing_type, "抽出対象の画像がありませんでした。")

    # 画像枚数の上限制御（多すぎる場合は先頭から間引く）
    pre_warnings: list[str] = []
    if len(images) > MAX_TOTAL_IMAGES:
        pre_warnings.append(
            f"画像が{len(images)}枚あり上限({MAX_TOTAL_IMAGES})を超えたため、"
            f"先頭{MAX_TOTAL_IMAGES}枚のみを使用しました。残りは別途確認してください。"
        )
        images = images[:MAX_TOTAL_IMAGES]

    # リクエスト合計サイズの上限制御（base64 合計が API 上限32MBを超えると413で全滅するため）
    kept: list[dict] = []
    total_b64 = 0
    for img in images:
        b64 = img.get("image_base64") or ""
        size = len(b64)
        if kept and total_b64 + size > MAX_REQUEST_BYTES:
            pre_warnings.append(
                f"画像の合計サイズが上限({MAX_REQUEST_BYTES // 1_000_000}MB)に達したため、"
                f"{len(kept)}枚のみを使用しました（残り{len(images) - len(kept)}枚は別途確認してください）。"
            )
            break
        kept.append(img)
        total_b64 += size
    images = kept or images[:1]

    # API キー確認（無ければフォールバック）
    try:
        from config import get_api_key
        api_key = get_api_key()
    except Exception as e:
        return _fallback_spec(drawing_type, f"設定の読み込みに失敗しました: {e}", pre_warnings)

    if not api_key:
        return _fallback_spec(
            drawing_type,
            "ANTHROPIC_API_KEY が未設定のため AI 抽出を実行できませんでした。"
            "確認フォームで手入力してください。",
            pre_warnings,
        )

    system_prompt = build_system_prompt()
    user_prompt = build_user_prompt(drawing_type=drawing_type, hint=hint)
    content = _build_content(images, user_prompt)

    last_error = None
    parsed: Optional[dict] = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            parsed = _call_claude_api(content, system_prompt, attempt)
            break
        except Exception as e:
            last_error = e
            wait = RETRY_DELAY_SEC * attempt
            logger.warning(f"Vision抽出 試行{attempt}/{MAX_RETRIES} 失敗: {e}. {wait}秒後リトライ。")
            if attempt < MAX_RETRIES:
                time.sleep(wait)

    if parsed is None:
        return _fallback_spec(
            drawing_type,
            f"AI抽出に{MAX_RETRIES}回失敗しました。確認フォームで手入力してください。詳細: {last_error}",
            pre_warnings,
        )

    # 正規化 → spec 復元
    try:
        normalized, norm_warnings, norm_conf = _normalize_parsed(parsed, drawing_type)
        spec = spec_from_dict(normalized)
    except Exception as e:
        logger.warning(f"抽出結果の復元に失敗: {e}")
        return _fallback_spec(
            drawing_type,
            f"AI抽出結果の解釈に失敗しました。確認フォームで手入力してください。詳細: {e}",
            pre_warnings,
        )

    spec.drawing_type = drawing_type

    # warnings 統合（前処理 + 正規化 + API由来）
    merged_warnings = list(pre_warnings)
    merged_warnings.extend(norm_warnings)
    for w in (spec.warnings or []):
        if w not in merged_warnings:
            merged_warnings.append(w)
    spec.warnings = merged_warnings

    # confidence 統合（API由来を優先しつつ、正規化と自動補完で "low" を埋める）
    auto_conf = _auto_confidence(spec)
    merged_conf = dict(auto_conf)
    merged_conf.update(norm_conf)
    merged_conf.update(spec.confidence or {})  # API が明示した値を最優先
    spec.confidence = merged_conf

    # 宣言された枚数/出力から total を整える（座標未確定でも target_panel_count から算出）
    spec.recompute_totals()
    return spec


def extract_drafting_spec(
    file_paths: list[str],
    *,
    drawing_type: str = DrawingType.LAYOUT,
    hint: str = "",
) -> DraftingSpec:
    """現調資料ファイル群（PDF/PNG/JPG 混在可）から DraftingSpec を抽出する。

    Args:
        file_paths: 入力ファイルパスのリスト（PDF・画像混在可）。
        drawing_type: 図面種別（"layout"/"string"/"equipment"）。
        hint: 補足ヒント文字列（施主名・既知メーカー等）。

    Returns:
        DraftingSpec。API キー無し／失敗時は warnings 入りの最小 spec を返す（例外は投げない）。
    """
    drawing_type = drawing_type if drawing_type in _VALID_DRAWING_TYPES else DrawingType.LAYOUT

    if not file_paths:
        return _fallback_spec(drawing_type, "入力ファイルが指定されていません。")

    images, load_warnings = _collect_images(file_paths)

    if not images:
        return _fallback_spec(
            drawing_type,
            "入力ファイルから画像を取得できませんでした。確認フォームで手入力してください。",
            load_warnings,
        )

    spec = extract_drafting_spec_from_images(images, drawing_type=drawing_type, hint=hint)

    # ファイル読み込み段階の warnings を先頭に追加（重複は避ける）
    if load_warnings:
        existing = spec.warnings or []
        spec.warnings = load_warnings + [w for w in existing if w not in load_warnings]
    return spec


# =============================================================
# 自己テスト
# =============================================================

def _self_test() -> bool:
    """ネットワーク不要の自己テスト。

    1) プロンプト文字列が生成できること。
    2) few-shot JSON が spec_from_dict で往復し、主要値が一致すること。
    3) API キー無しフォールバックが例外なく最小 spec を返すこと。
    4) API キーがあれば（任意・実ファイル1件で）実抽出を試す。
    """
    print("=== drafting.spec_extractor 自己テスト ===")
    ok = True

    # --- 1) プロンプト生成 ---
    try:
        sysp = build_system_prompt()
        userp = build_user_prompt(drawing_type="layout", hint="施主は法人")
        userp_str = build_user_prompt(drawing_type="string")
        assert isinstance(sysp, str) and len(sysp) > 50
        assert "DraftingSpec" in userp and "正解出力例" in userp
        assert 'drawing_type="string"' in userp_str
        print(f"[OK] プロンプト生成: system={len(sysp)}文字 / user(layout)={len(userp)}文字")
    except Exception as e:
        ok = False
        print(f"[NG] プロンプト生成に失敗: {e}")

    # --- 2) few-shot JSON が spec_from_dict で往復 ---
    try:
        for name in ("kurihara_layout", "tok_string"):
            golden = get_golden(name)
            example_json = _golden_example_json(name)
            parsed = json.loads(example_json)
            restored = spec_from_dict(parsed)
            assert restored.customer_name == golden.customer_name, f"{name}: 施主名不一致"
            assert restored.panel.model == golden.panel.model, f"{name}: 型番不一致"
            assert len(restored.roof_faces) == len(golden.roof_faces), f"{name}: 面数不一致"
            assert len(restored.strings) == len(golden.strings), f"{name}: 系統数不一致"
        print("[OK] few-shot JSON 往復: kurihara_layout / tok_string が spec_from_dict で復元一致")
    except Exception as e:
        ok = False
        print(f"[NG] few-shot 往復に失敗: {e}")

    # --- 3) APIキー無しフォールバック（強制的にキーを空にしてテスト）---
    try:
        saved = os.environ.get("ANTHROPIC_API_KEY")
        os.environ["ANTHROPIC_API_KEY"] = ""
        try:
            # ダミー画像 dict（小さな 1x1 png base64）を1枚渡す
            dummy_png_b64 = (
                "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
            )
            dummy_images = [{
                "page": 1,
                "image_base64": dummy_png_b64,
                "image_bytes": base64.b64decode(dummy_png_b64),
                "media_type": "image/png",
            }]
            spec_nokey = extract_drafting_spec_from_images(dummy_images, drawing_type="layout")
            assert isinstance(spec_nokey, DraftingSpec), "DraftingSpec が返らない"
            assert spec_nokey.customer_name == "", "フォールバックは customer_name 空のはず"
            assert spec_nokey.warnings, "フォールバックは warnings を持つはず"
            assert spec_nokey.drawing_type == "layout"

            # ファイルパス経由の異常系（存在しないファイル）も例外を投げないこと
            spec_badfile = extract_drafting_spec(["/no/such/file.pdf"], drawing_type="string")
            assert isinstance(spec_badfile, DraftingSpec)
            assert spec_badfile.warnings, "存在しないファイルでも warnings 入り spec を返すはず"

            # 空入力
            spec_empty = extract_drafting_spec([])
            assert isinstance(spec_empty, DraftingSpec) and spec_empty.warnings
        finally:
            if saved is None:
                os.environ.pop("ANTHROPIC_API_KEY", None)
            else:
                os.environ["ANTHROPIC_API_KEY"] = saved
        print("[OK] キー無し/異常系フォールバック: 例外なく warnings 入り最小 spec を返す")
    except Exception as e:
        ok = False
        print(f"[NG] フォールバックに失敗: {e}")

    # --- 4) 任意: 実APIキーがあれば実ファイルで試す（ネットワーク要・失敗してもテストは失敗扱いにしない）---
    try:
        from config import get_api_key
        if get_api_key():
            import glob
            candidates = []
            for pat in ("sample/**/*.pdf", "見積AI入力資料*/**/*.pdf", "**/*太陽光*配置図*.pdf"):
                candidates.extend(glob.glob(pat, recursive=True))
            candidates = [c for c in candidates if os.path.isfile(c)][:1]
            if candidates:
                print(f"[INFO] 実APIキー検出。実ファイルで抽出を試行: {candidates[0]}")
                spec_real = extract_drafting_spec(candidates, drawing_type="layout")
                print(f"       → customer_name={spec_real.customer_name!r} "
                      f"面数={len(spec_real.roof_faces)} "
                      f"warnings={len(spec_real.warnings)}件")
            else:
                print("[INFO] 実APIキーはあるがテスト用サンプルPDFが見つからないため実抽出はスキップ。")
        else:
            print("[INFO] 実APIキー無し。実抽出テストはスキップ（フォールバックパスのみ検証済）。")
    except Exception as e:
        # ネットワーク/サンプル都合の失敗はテスト結果に影響させない
        print(f"[INFO] 実抽出（任意）はスキップ/失敗: {e}")

    print("=== 結果:", "全パス" if ok else "一部失敗", "===")
    return ok


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    success = _self_test()
    raise SystemExit(0 if success else 1)
