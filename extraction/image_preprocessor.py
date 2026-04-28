"""手書きOCR・図面OCR向けの積極的な画像前処理パイプライン

`extraction/pdf_reader.py` の `_apply_image_enhancement()` は控えめな処理だが、
本モジュールはスマホ撮影PDFやスキャナの傾きに強い、より積極的な前処理を提供する。
Claude Vision API へ送る前段でコールすることで手書きOCR精度を向上させる。

依存ライブラリ: Pillow（必須）、numpy（任意。傾き補正・背景除去・統計判定で使用）
numpy が入っていない環境では自動的にフォールバック動作（傾き補正等をスキップ）に切り替わる。

使用例:
    >>> from extraction.image_preprocessor import (
    ...     enhance_for_handwriting_ocr,
    ...     enhance_for_diagram_ocr,
    ...     auto_select_pipeline,
    ... )
    >>>
    >>> # 手書き現調シート向け
    >>> with open("survey.png", "rb") as f:
    ...     png_bytes = f.read()
    >>> enhanced_bytes, media_type = enhance_for_handwriting_ocr(png_bytes)
    >>>
    >>> # 単線結線図・配置図向け
    >>> enhanced_bytes, media_type = enhance_for_diagram_ocr(png_bytes)
    >>>
    >>> # 内容を簡易判定して自動振り分け
    >>> enhanced_bytes, media_type = auto_select_pipeline(png_bytes)

設計方針（副作用なし保証）:
    すべての関数は処理が失敗しても例外を上げず、入力画像をそのまま返す。
    呼び出し側は安心して既存パイプラインに差し込める。
"""
from __future__ import annotations

import io
import logging
from typing import Optional

from PIL import Image, ImageEnhance, ImageFilter, ImageOps

logger = logging.getLogger(__name__)

# numpy はオプション依存。無い場合は傾き補正・背景除去・統計判定をスキップする。
try:
    import numpy as _np  # type: ignore

    _HAS_NUMPY = True
except Exception:  # pragma: no cover - 環境依存
    _np = None  # type: ignore
    _HAS_NUMPY = False


# ---------------------------------------------------------------------------
# 公開API
# ---------------------------------------------------------------------------
def enhance_for_handwriting_ocr(image_bytes: bytes) -> tuple[bytes, str]:
    """手書きOCR向けの積極的な画像強化を行う。

    パイプライン:
        1. グレースケール変換（L mode）
        2. 傾き補正（deskew、±5°以内の自動回転）— numpyが利用可能な場合
        3. ノイズ除去（メディアンフィルタ size=3）
        4. 背景除去（ガウシアンブラー差分による紙の薄汚れ除去）— numpyが利用可能な場合
        5. オートコントラスト（cutoff=2）
        6. コントラスト 1.4 倍
        7. シャープネス 1.3 倍

    Args:
        image_bytes: 入力画像のバイト列（PNG/JPEG等、PILで開ける形式）

    Returns:
        (処理後のPNGバイト列, "image/png")
        失敗した場合は (元のバイト列, "image/png")
    """
    try:
        pil_img = _to_pil(image_bytes)
        if pil_img is None:
            return image_bytes, "image/png"

        # 1. グレースケール
        try:
            gray = pil_img.convert("L")
        except Exception:
            gray = pil_img

        # 2. 傾き補正（numpy必須）
        if _HAS_NUMPY:
            try:
                angle = detect_skew_angle(gray)
                if abs(angle) >= 0.5:  # 0.5°未満は誤差扱いで補正しない
                    gray = gray.rotate(
                        angle, resample=Image.BICUBIC, fillcolor=255, expand=False
                    )
            except Exception as e:
                logger.debug(f"deskew失敗（スキップ）: {e}")

        # 3. メディアンフィルタでノイズ除去
        try:
            gray = gray.filter(ImageFilter.MedianFilter(size=3))
        except Exception as e:
            logger.debug(f"メディアンフィルタ失敗（スキップ）: {e}")

        # 4. 背景除去（ガウシアンブラー差分）— 紙の薄汚れ・影むらを消す
        if _HAS_NUMPY:
            try:
                gray = _remove_background(gray)
            except Exception as e:
                logger.debug(f"背景除去失敗（スキップ）: {e}")

        # 5. オートコントラスト
        try:
            gray = ImageOps.autocontrast(gray, cutoff=2)
        except Exception as e:
            logger.debug(f"オートコントラスト失敗（スキップ）: {e}")

        # 6. コントラスト1.4倍
        try:
            gray = ImageEnhance.Contrast(gray).enhance(1.4)
        except Exception as e:
            logger.debug(f"コントラスト強調失敗（スキップ）: {e}")

        # 7. シャープネス1.3倍
        try:
            gray = ImageEnhance.Sharpness(gray).enhance(1.3)
        except Exception as e:
            logger.debug(f"シャープネス強調失敗（スキップ）: {e}")

        return _to_png_bytes(gray), "image/png"
    except Exception as e:
        logger.warning(f"enhance_for_handwriting_ocr 失敗、元画像を返却: {e}")
        return image_bytes, "image/png"


def enhance_for_diagram_ocr(image_bytes: bytes) -> tuple[bytes, str]:
    """図面（配管図・単線結線図・配置図）向けの控えめな画像強化を行う。

    線が潰れないようエッジは強調しすぎず、軽微な前処理に留める。

    パイプライン:
        1. RGB統一（カラーの線種が情報を持つ図面を想定）
        2. 軽微なノイズ除去（メディアンフィルタ size=3）
        3. オートコントラスト（cutoff=1）
        4. コントラスト 1.2 倍
        5. シャープネス 1.1 倍（軽微）

    Returns:
        (処理後のPNGバイト列, "image/png")
        失敗した場合は (元のバイト列, "image/png")
    """
    try:
        pil_img = _to_pil(image_bytes)
        if pil_img is None:
            return image_bytes, "image/png"

        # 1. RGBに統一
        try:
            if pil_img.mode not in ("RGB", "L"):
                pil_img = pil_img.convert("RGB")
        except Exception:
            pass

        # 2. 軽微なメディアンフィルタ
        try:
            pil_img = pil_img.filter(ImageFilter.MedianFilter(size=3))
        except Exception as e:
            logger.debug(f"メディアンフィルタ失敗（スキップ）: {e}")

        # 3. オートコントラスト
        try:
            if pil_img.mode in ("RGB", "L"):
                pil_img = ImageOps.autocontrast(pil_img, cutoff=1)
        except Exception as e:
            logger.debug(f"オートコントラスト失敗（スキップ）: {e}")

        # 4. コントラスト1.2倍
        try:
            pil_img = ImageEnhance.Contrast(pil_img).enhance(1.2)
        except Exception as e:
            logger.debug(f"コントラスト強調失敗（スキップ）: {e}")

        # 5. シャープネス1.1倍
        try:
            pil_img = ImageEnhance.Sharpness(pil_img).enhance(1.1)
        except Exception as e:
            logger.debug(f"シャープネス強調失敗（スキップ）: {e}")

        return _to_png_bytes(pil_img), "image/png"
    except Exception as e:
        logger.warning(f"enhance_for_diagram_ocr 失敗、元画像を返却: {e}")
        return image_bytes, "image/png"


def auto_select_pipeline(image_bytes: bytes) -> tuple[bytes, str]:
    """画素統計から内容を簡易判定して、手書き向け or 図面向けを自動選択する。

    判定ロジック:
        - 暗ピクセル比率（黒画素率）が高い かつ エッジ密度が中程度以上 → 手書き
        - 暗ピクセル比率が低く、エッジ密度が高い → 線画中心の図面
        - 上記以外 → 手書き向け（保守的なデフォルト）

    numpyが無い場合はPILヒストグラムによる簡易判定にフォールバックする。

    Returns:
        (処理後のPNGバイト列, "image/png")
        失敗した場合は (元のバイト列, "image/png")
    """
    try:
        pil_img = _to_pil(image_bytes)
        if pil_img is None:
            return image_bytes, "image/png"

        is_diagram = _looks_like_diagram(pil_img)
        if is_diagram:
            return enhance_for_diagram_ocr(image_bytes)
        return enhance_for_handwriting_ocr(image_bytes)
    except Exception as e:
        logger.warning(f"auto_select_pipeline 失敗、手書きパイプラインで継続: {e}")
        try:
            return enhance_for_handwriting_ocr(image_bytes)
        except Exception:
            return image_bytes, "image/png"


# ---------------------------------------------------------------------------
# 補助関数
# ---------------------------------------------------------------------------
def detect_skew_angle(pil_img: Image.Image) -> float:
    """簡易deskew: 水平投影プロファイルの分散を最大化する角度を-5°〜+5°でスキャンする。

    手順:
        1. グレースケール化 → numpy配列化
        2. 大津法ライクに平均輝度で二値化（暗画素=テキスト=1, 明画素=0）
        3. -5°〜+5° を 0.5° 刻みで回転し、各角度で行ごとの黒画素数（水平射影プロファイル）の分散を計算
        4. 分散が最大となる角度を返す（テキスト行が水平に揃っているとき分散が最大化される）

    numpyが無い、または失敗した場合は 0.0 を返す。

    Args:
        pil_img: PIL Image（モード問わず内部でLに変換）

    Returns:
        推定された傾き角度（度、回転で打ち消す方向の符号）。失敗時は 0.0。
    """
    if not _HAS_NUMPY:
        return 0.0
    try:
        if pil_img.mode != "L":
            gray = pil_img.convert("L")
        else:
            gray = pil_img

        # 計算量を抑えるため長辺1000pxまでに縮小
        max_dim = 1000
        w, h = gray.size
        if max(w, h) > max_dim:
            scale = max_dim / max(w, h)
            gray = gray.resize(
                (max(1, int(w * scale)), max(1, int(h * scale))), Image.BILINEAR
            )

        arr = _np.asarray(gray, dtype=_np.uint8)
        if arr.size == 0:
            return 0.0

        # 二値化: しきい値=平均-10（テキストは平均より暗いとみなす）
        threshold = max(1, int(arr.mean()) - 10)
        binary = (arr < threshold).astype(_np.uint8)

        # 黒画素がほぼ無い/全部黒 のときは判定不能
        ratio = float(binary.mean())
        if ratio < 0.005 or ratio > 0.95:
            return 0.0

        best_angle = 0.0
        best_score = -1.0

        # -5° 〜 +5° を 0.5° 刻みでスキャン
        angles = [a * 0.5 for a in range(-10, 11)]
        for angle in angles:
            try:
                # PIL.rotate は度数指定。fillcolor=0で背景は黒画素扱い
                rotated = Image.fromarray(binary * 255).rotate(
                    angle, resample=Image.BILINEAR, fillcolor=0, expand=False
                )
                rot_arr = _np.asarray(rotated, dtype=_np.uint8)
                projection = rot_arr.sum(axis=1).astype(_np.float64)
                # 分散が大きい = 行ごとに黒画素数が偏っている = テキスト行が水平
                score = float(projection.var())
                if score > best_score:
                    best_score = score
                    best_angle = float(angle)
            except Exception:
                continue

        # 検出した傾きを打ち消す方向に回転するため、符号は反転して返す
        return -best_angle
    except Exception as e:
        logger.debug(f"detect_skew_angle 失敗: {e}")
        return 0.0


def _to_pil(image_bytes: bytes) -> Optional[Image.Image]:
    """バイト列を PIL Image に変換する内部ヘルパー。失敗時は None。"""
    try:
        img = Image.open(io.BytesIO(image_bytes))
        img.load()  # 遅延ロードを強制してファイルハンドルを閉じる
        return img
    except Exception as e:
        logger.warning(f"画像のデコードに失敗: {e}")
        return None


def _to_png_bytes(pil_img: Image.Image) -> bytes:
    """PIL Image を PNG バイト列に変換する。"""
    buf = io.BytesIO()
    pil_img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def _remove_background(gray: Image.Image) -> Image.Image:
    """ガウシアンブラー差分による背景除去（紙の薄汚れ・影むらを消す）。

    手順:
        1. 大きいガウシアンブラーで「背景画像」を作成
        2. 元画像との差分を取り、明るさを補正
        3. 文字（暗部）を強調しつつ背景のムラを白に近づける

    numpy必須。失敗時は入力をそのまま返す。
    """
    if not _HAS_NUMPY:
        return gray
    try:
        if gray.mode != "L":
            gray = gray.convert("L")

        # 強めのブラーで背景マップを作成
        background = gray.filter(ImageFilter.GaussianBlur(radius=21))

        arr = _np.asarray(gray, dtype=_np.int16)
        bg = _np.asarray(background, dtype=_np.int16)

        # 差分を取って255に正規化（255 - (bg - fg)） => 背景から暗い分だけ暗くする
        diff = 255 - (bg - arr)
        diff = _np.clip(diff, 0, 255).astype(_np.uint8)

        return Image.fromarray(diff, mode="L")
    except Exception as e:
        logger.debug(f"_remove_background 失敗: {e}")
        return gray


def _looks_like_diagram(pil_img: Image.Image) -> bool:
    """画素統計から「図面っぽい画像」かどうか簡易判定する。

    判定基準:
        - 暗画素比率（dark_ratio）: 8%未満 → 線画的（図面寄り）
        - エッジ密度（edge_ratio）: 高め → 線が多い

    numpyが無い場合は PIL の getextrema/histogram で簡易判定。
    """
    try:
        if pil_img.mode != "L":
            gray = pil_img.convert("L")
        else:
            gray = pil_img

        # サンプル用に縮小
        max_dim = 600
        w, h = gray.size
        if max(w, h) > max_dim:
            scale = max_dim / max(w, h)
            gray = gray.resize(
                (max(1, int(w * scale)), max(1, int(h * scale))), Image.BILINEAR
            )

        # エッジ抽出（PILのFIND_EDGES）
        edges = gray.filter(ImageFilter.FIND_EDGES)

        if _HAS_NUMPY:
            arr = _np.asarray(gray, dtype=_np.uint8)
            edge_arr = _np.asarray(edges, dtype=_np.uint8)
            if arr.size == 0:
                return False
            # 暗画素率（しきい値128未満を暗とみなす）
            dark_ratio = float((arr < 128).mean())
            # エッジ密度（エッジ強度50以上を「エッジ」とみなす）
            edge_ratio = float((edge_arr > 50).mean())
        else:
            # numpyなしフォールバック: ヒストグラムから推定
            hist = gray.histogram()  # 256 bins
            total = sum(hist) or 1
            dark_ratio = sum(hist[:128]) / total
            edge_hist = edges.histogram()
            edge_total = sum(edge_hist) or 1
            edge_ratio = sum(edge_hist[50:]) / edge_total

        # 線画的: 暗画素が少なく、エッジは一定数ある
        # 手書き的: 暗画素（インクの塊）がそれなりにある
        if dark_ratio < 0.08 and edge_ratio > 0.02:
            return True
        return False
    except Exception as e:
        logger.debug(f"_looks_like_diagram 失敗（手書き扱いにフォールバック）: {e}")
        return False
