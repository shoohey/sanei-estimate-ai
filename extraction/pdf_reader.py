"""PDF→画像変換（PyMuPDF）"""
import base64
import io
import fitz  # PyMuPDF
from PIL import Image

# Claude API の画像サイズ上限（base64エンコード前）
MAX_IMAGE_BYTES = 4_500_000  # 4.5MB（5MB上限に余裕を持たせる）


def pdf_to_images(pdf_path: str, dpi: int = 200) -> list[dict]:
    """PDFの各ページをPNG画像に変換してbase64エンコード

    大きい画像はJPEG圧縮・リサイズで5MB以内に収める。

    Args:
        pdf_path: PDFファイルパス
        dpi: 解像度（デフォルト200dpi）

    Returns:
        list of {"page": int, "image_base64": str, "image_bytes": bytes,
                 "media_type": str}
    """
    doc = fitz.open(pdf_path)
    pages = []

    for page_num in range(len(doc)):
        page = doc[page_num]
        zoom = dpi / 72
        mat = fitz.Matrix(zoom, zoom)
        pix = page.get_pixmap(matrix=mat)
        img_bytes = pix.tobytes("png")

        # サイズチェック：大きすぎる場合はJPEG圧縮
        if len(img_bytes) > MAX_IMAGE_BYTES:
            img_bytes, media_type = _compress_image(pix, MAX_IMAGE_BYTES)
        else:
            media_type = "image/png"

        img_base64 = base64.standard_b64encode(img_bytes).decode("utf-8")

        pages.append({
            "page": page_num + 1,
            "image_base64": img_base64,
            "image_bytes": img_bytes,
            "media_type": media_type,
        })

    doc.close()
    return pages


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
