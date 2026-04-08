"""PDF→画像変換（PyMuPDF）"""
import base64
import io
import logging
import fitz  # PyMuPDF
from PIL import Image, ImageEnhance, ImageOps

logger = logging.getLogger(__name__)

# Claude API の画像サイズ上限（base64エンコード前）
MAX_IMAGE_BYTES = 4_500_000  # 4.5MB（5MB上限に余裕を持たせる）


def pdf_to_images(
    pdf_path: str,
    dpi: int = 200,
    auto_rotate: bool = True,
    enhance_contrast: bool = True,
) -> list[dict]:
    """PDFの各ページをPNG画像に変換してbase64エンコード

    大きい画像はJPEG圧縮・リサイズで5MB以内に収める。
    PDFのページ回転メタデータがある場合は自動補正し、
    手書きOCR精度向上のため軽微なコントラスト強調を適用する（副作用なし）。

    Args:
        pdf_path: PDFファイルパス
        dpi: 解像度（デフォルト200dpi）
        auto_rotate: PDFのページ回転メタデータに従って自動回転する
        enhance_contrast: 手書きOCRのために軽微なコントラスト強調を適用する

    Returns:
        list of {"page": int, "image_base64": str, "image_bytes": bytes,
                 "media_type": str}
    """
    try:
        doc = fitz.open(pdf_path)
    except Exception as e:
        raise RuntimeError(f"PDFファイルを開けません（ファイル破損またはパスワード保護の可能性）: {e}") from e

    if len(doc) == 0:
        doc.close()
        raise RuntimeError("PDFにページがありません。空のPDFファイルです。")

    pages = []

    for page_num in range(len(doc)):
        page = doc[page_num]

        # PDFのページ回転情報を取得（auto_rotateがTrueの場合のみ補正）
        # PyMuPDFはページのrotationプロパティを持つ。get_pixmap時にmatrixで回転を適用する。
        rotation = 0
        if auto_rotate:
            try:
                rotation = page.rotation or 0
            except Exception:
                rotation = 0

        # まずデフォルトDPIで試行、失敗したらDPIを下げてリトライ
        img_bytes = None
        media_type = "image/png"
        pix = None

        for try_dpi in [dpi, 150, 100]:
            try:
                zoom = try_dpi / 72
                mat = fitz.Matrix(zoom, zoom)
                # 回転補正（rotationが0でなければ適用）
                if rotation:
                    mat = mat.prerotate(rotation)
                pix = page.get_pixmap(matrix=mat, alpha=False)
                img_bytes = pix.tobytes("png")
                break
            except Exception as e:
                logger.warning(f"ページ{page_num + 1}のレンダリングエラー（DPI={try_dpi}）: {e}")
                continue

        if img_bytes is None:
            logger.error(f"ページ{page_num + 1}のレンダリングに完全に失敗しました。スキップします。")
            continue

        # コントラスト強調（手書きOCRの精度向上のため軽微に適用）
        if enhance_contrast:
            try:
                img_bytes, media_type = _apply_image_enhancement(img_bytes)
            except Exception as e:
                # 失敗しても元画像で継続（副作用を起こさない）
                logger.warning(f"ページ{page_num + 1}のコントラスト調整に失敗しました: {e}")

        # サイズチェック：大きすぎる場合はJPEG圧縮
        if len(img_bytes) > MAX_IMAGE_BYTES:
            if pix is not None:
                img_bytes, media_type = _compress_image(pix, MAX_IMAGE_BYTES)
            else:
                img_bytes, media_type = _compress_pil_image(img_bytes, MAX_IMAGE_BYTES)

        img_base64 = base64.standard_b64encode(img_bytes).decode("utf-8")

        pages.append({
            "page": page_num + 1,
            "image_base64": img_base64,
            "image_bytes": img_bytes,
            "media_type": media_type,
        })

    doc.close()
    return pages


def _apply_image_enhancement(img_bytes: bytes) -> tuple[bytes, str]:
    """画像に軽微なコントラスト強調を適用する。

    手書きOCRの精度向上を目的とした控えめな前処理:
    - コントラスト1.15倍: 薄い手書き文字を強調
    - シャープネス1.1倍: エッジを鮮明化
    - オートコントラスト: ヒストグラムの端をわずかに切り詰める

    Returns:
        (処理後のPNGバイト列, メディアタイプ)
    """
    pil_img = Image.open(io.BytesIO(img_bytes))

    # RGBモードに統一（モノクロ画像でもエンハンス処理を適用可能に）
    if pil_img.mode not in ("RGB", "L"):
        pil_img = pil_img.convert("RGB")

    # オートコントラスト（ヒストグラム最両端の1%ずつをカット）
    try:
        if pil_img.mode == "RGB":
            pil_img = ImageOps.autocontrast(pil_img, cutoff=1)
    except Exception:
        pass

    # コントラスト強調（1.15倍、控えめ）
    enhancer = ImageEnhance.Contrast(pil_img)
    pil_img = enhancer.enhance(1.15)

    # シャープネス強調（1.1倍、控えめ）
    enhancer = ImageEnhance.Sharpness(pil_img)
    pil_img = enhancer.enhance(1.1)

    buf = io.BytesIO()
    pil_img.save(buf, format="PNG", optimize=True)
    return buf.getvalue(), "image/png"


def _compress_pil_image(img_bytes: bytes, max_bytes: int) -> tuple[bytes, str]:
    """PIL経由でJPEG圧縮する（_compress_imageのPIL版）。

    _apply_image_enhancement でPNGバイト列に変換された後、
    サイズ超過時にJPEG圧縮するために使用する。
    """
    pil_img = Image.open(io.BytesIO(img_bytes))

    # まずリサイズ（長辺を最大2000pxに）
    max_dim = 2000
    w, h = pil_img.size
    if max(w, h) > max_dim:
        scale = max_dim / max(w, h)
        new_w = int(w * scale)
        new_h = int(h * scale)
        pil_img = pil_img.resize((new_w, new_h), Image.LANCZOS)

    # JPEG圧縮
    for quality in [90, 80, 70, 60, 50]:
        buf = io.BytesIO()
        pil_img.convert("RGB").save(buf, format="JPEG", quality=quality)
        jpeg_bytes = buf.getvalue()
        if len(jpeg_bytes) <= max_bytes:
            return jpeg_bytes, "image/jpeg"

    # さらにリサイズ
    for max_dim in [1500, 1200, 1000]:
        w, h = pil_img.size
        scale = max_dim / max(w, h)
        resized = pil_img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
        buf = io.BytesIO()
        resized.convert("RGB").save(buf, format="JPEG", quality=70)
        jpeg_bytes = buf.getvalue()
        if len(jpeg_bytes) <= max_bytes:
            return jpeg_bytes, "image/jpeg"

    # 最終手段
    buf = io.BytesIO()
    pil_img.convert("RGB").save(buf, format="JPEG", quality=40)
    return buf.getvalue(), "image/jpeg"


def _compress_image(pix, max_bytes: int) -> tuple[bytes, str]:
    """画像をJPEG圧縮してサイズ上限内に収める"""
    # PyMuPDF pixmapからPIL Imageに変換
    img_data = pix.tobytes("png")
    pil_img = Image.open(io.BytesIO(img_data))

    # まずリサイズ（長辺を最大2000pxに）
    max_dim = 2000
    w, h = pil_img.size
    if max(w, h) > max_dim:
        scale = max_dim / max(w, h)
        new_w = int(w * scale)
        new_h = int(h * scale)
        pil_img = pil_img.resize((new_w, new_h), Image.LANCZOS)

    # JPEG圧縮（品質を段階的に下げる）
    for quality in [90, 80, 70, 60, 50]:
        buf = io.BytesIO()
        pil_img.convert("RGB").save(buf, format="JPEG", quality=quality)
        jpeg_bytes = buf.getvalue()
        if len(jpeg_bytes) <= max_bytes:
            return jpeg_bytes, "image/jpeg"

    # それでも大きい場合はさらにリサイズ
    for max_dim in [1500, 1200, 1000]:
        w, h = pil_img.size
        scale = max_dim / max(w, h)
        resized = pil_img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
        buf = io.BytesIO()
        resized.convert("RGB").save(buf, format="JPEG", quality=70)
        jpeg_bytes = buf.getvalue()
        if len(jpeg_bytes) <= max_bytes:
            return jpeg_bytes, "image/jpeg"

    # 最終手段
    buf = io.BytesIO()
    pil_img.convert("RGB").save(buf, format="JPEG", quality=40)
    return buf.getvalue(), "image/jpeg"


def pdf_page_count(pdf_path: str) -> int:
    """PDFのページ数を取得"""
    doc = fitz.open(pdf_path)
    count = len(doc)
    doc.close()
    return count
