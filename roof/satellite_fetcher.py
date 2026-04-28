"""住所→緯度経度→衛星画像を取得するモジュール。

太陽光パネルのレイアウト計算のために、住所から屋根の衛星画像を取得する。
Google Maps API（Geocoding + Static Maps）を優先し、APIキーが無ければ
OpenStreetMap Nominatim + Esri World Imagery タイルへフォールバックする。

使用例:
    from roof.satellite_fetcher import get_roof_view

    view = get_roof_view("東京都新宿区西新宿2-8-1")
    if view["error"] is None:
        with open("/tmp/roof.png", "wb") as f:
            f.write(view["image_bytes"])
        print(
            f"Lat/Lng: {view['lat']}, {view['lng']}, "
            f"Scale: {view['scale_meter_per_pixel']:.3f} m/px"
        )
"""

from __future__ import annotations

import io
import math
import os
import urllib.parse
from typing import Optional, Tuple

import requests

try:
    from PIL import Image
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "Pillow が必要です。requirements.txt の Pillow>=10.0.0 を確認してください。"
    ) from exc


# ---------------------------------------------------------------------------
# 定数
# ---------------------------------------------------------------------------

_DEFAULT_TIMEOUT = 10  # 秒
_USER_AGENT = "sanae-estimate-ai/2.2 (https://github.com/shoohey/sanei-estimate-ai)"

_GOOGLE_GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"
_GOOGLE_STATICMAP_URL = "https://maps.googleapis.com/maps/api/staticmap"
_NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
_ESRI_TILE_URL = (
    "https://server.arcgisonline.com/ArcGIS/rest/services/"
    "World_Imagery/MapServer/tile/{z}/{y}/{x}"
)

_TILE_SIZE = 256  # Esriタイルの1辺ピクセル数


# ---------------------------------------------------------------------------
# 補助関数
# ---------------------------------------------------------------------------


def _get_google_api_key() -> Optional[str]:
    """環境変数および streamlit secrets から Google Maps API キーを取得する。

    優先順位:
      1. 環境変数 GOOGLE_MAPS_API_KEY
      2. streamlit.secrets["GOOGLE_MAPS_API_KEY"]（streamlit実行時のみ）
    """
    key = os.environ.get("GOOGLE_MAPS_API_KEY")
    if key:
        return key.strip() or None

    # streamlit secrets からの取得（streamlit が無い/未設定でも例外を握る）
    try:
        import streamlit as st  # type: ignore

        try:
            value = st.secrets.get("GOOGLE_MAPS_API_KEY")  # type: ignore[attr-defined]
        except Exception:
            # secrets ファイルが無い場合などに例外になる
            value = None
        if value:
            return str(value).strip() or None
    except Exception:
        pass

    return None


def _zoom_to_meter_per_pixel(lat: float, zoom: int) -> float:
    """ズームレベルから 1ピクセルあたりのメートル数を計算する。

    Web メルカトル投影での近似式:
      meter_per_pixel = 156543.03 * cos(lat) / 2^zoom
    """
    return 156543.03 * math.cos(math.radians(lat)) / (2 ** zoom)


def _lat_lng_to_tile(lat: float, lng: float, zoom: int) -> Tuple[int, int]:
    """緯度経度を XYZ タイル座標 (x, y) に変換する。"""
    lat_rad = math.radians(lat)
    n = 2.0 ** zoom
    x = int((lng + 180.0) / 360.0 * n)
    y = int(
        (1.0 - math.log(math.tan(lat_rad) + 1.0 / math.cos(lat_rad)) / math.pi)
        / 2.0
        * n
    )
    # 範囲クランプ
    x = max(0, min(int(n) - 1, x))
    y = max(0, min(int(n) - 1, y))
    return x, y


def _lat_lng_to_tile_pixel(
    lat: float, lng: float, zoom: int
) -> Tuple[int, int, float, float]:
    """緯度経度→タイル座標 + タイル内のサブピクセル位置を返す。

    Returns:
        (tile_x, tile_y, frac_x, frac_y)
        frac_x, frac_y は 0〜1（タイル内の相対位置）
    """
    lat_rad = math.radians(lat)
    n = 2.0 ** zoom
    fx = (lng + 180.0) / 360.0 * n
    fy = (
        (1.0 - math.log(math.tan(lat_rad) + 1.0 / math.cos(lat_rad)) / math.pi)
        / 2.0
        * n
    )
    tx = int(fx)
    ty = int(fy)
    return tx, ty, fx - tx, fy - ty


def _compose_tiles_from_esri(lat: float, lng: float, zoom: int) -> bytes:
    """Esri World Imagery のタイルを 2x2 で取得し、PNGバイト列にして返す。

    ターゲット地点ができるだけ中心に来るよう、サブピクセル位置から
    左上タイルのオフセットを決定し、512x512 にクロップする。
    """
    tx, ty, fx, fy = _lat_lng_to_tile_pixel(lat, lng, zoom)

    # 中心地点を画像中央に置きたいので、左上タイルを (tx-1 or tx) / (ty-1 or ty)
    # から選ぶ。サブピクセル位置が 0.5 未満なら左上を一つ前のタイルにする。
    base_x = tx - 1 if fx < 0.5 else tx
    base_y = ty - 1 if fy < 0.5 else ty

    # 4タイル取得
    canvas = Image.new("RGB", (_TILE_SIZE * 2, _TILE_SIZE * 2), (0, 0, 0))
    headers = {"User-Agent": _USER_AGENT}
    last_error: Optional[str] = None
    fetched = 0

    for dy in range(2):
        for dx in range(2):
            x = base_x + dx
            y = base_y + dy
            url = _ESRI_TILE_URL.format(z=zoom, x=x, y=y)
            try:
                resp = requests.get(url, headers=headers, timeout=_DEFAULT_TIMEOUT)
                resp.raise_for_status()
                tile_img = Image.open(io.BytesIO(resp.content)).convert("RGB")
                canvas.paste(tile_img, (dx * _TILE_SIZE, dy * _TILE_SIZE))
                fetched += 1
            except Exception as e:
                last_error = f"tile ({x},{y},z={zoom}) fetch failed: {e}"

    if fetched == 0:
        raise RuntimeError(f"Esri タイル取得に全て失敗: {last_error}")

    # 中心が画像中央(512,512)に来るようクロップ。
    # 中心地点はキャンバス上で
    #   center_px_x = (tx - base_x + fx) * TILE_SIZE
    #   center_px_y = (ty - base_y + fy) * TILE_SIZE
    center_px_x = int((tx - base_x + fx) * _TILE_SIZE)
    center_px_y = int((ty - base_y + fy) * _TILE_SIZE)

    crop_size = 512
    half = crop_size // 2
    left = max(0, min(_TILE_SIZE * 2 - crop_size, center_px_x - half))
    top = max(0, min(_TILE_SIZE * 2 - crop_size, center_px_y - half))
    cropped = canvas.crop((left, top, left + crop_size, top + crop_size))

    buf = io.BytesIO()
    cropped.save(buf, format="PNG")
    return buf.getvalue()


# ---------------------------------------------------------------------------
# 公開関数
# ---------------------------------------------------------------------------


def geocode_address(address: str) -> dict:
    """住所文字列を緯度経度に変換する。

    Args:
        address: 住所文字列（例: "東京都新宿区西新宿2-8-1"）

    Returns:
        {
            "lat": float,
            "lng": float,
            "formatted_address": str,
            "source": "google" | "nominatim" | "error",
            "error": str | None,
        }
    """
    result = {
        "lat": None,
        "lng": None,
        "formatted_address": "",
        "source": "error",
        "error": None,
    }

    if not address or not address.strip():
        result["error"] = "address is empty"
        return result

    address = address.strip()

    # 1) Google Geocoding API
    api_key = _get_google_api_key()
    if api_key:
        try:
            params = {
                "address": address,
                "key": api_key,
                "language": "ja",
            }
            resp = requests.get(
                _GOOGLE_GEOCODE_URL, params=params, timeout=_DEFAULT_TIMEOUT
            )
            resp.raise_for_status()
            data = resp.json()
            status = data.get("status")
            if status == "OK" and data.get("results"):
                top = data["results"][0]
                loc = top["geometry"]["location"]
                return {
                    "lat": float(loc["lat"]),
                    "lng": float(loc["lng"]),
                    "formatted_address": top.get("formatted_address", address),
                    "source": "google",
                    "error": None,
                }
            else:
                # ステータスが OK でなければフォールバックへ
                google_error = (
                    f"Google Geocoding status={status} "
                    f"error_message={data.get('error_message', '')}"
                )
        except Exception as e:
            google_error = f"Google Geocoding exception: {e}"
        # 失敗時は Nominatim へフォールバック
    else:
        google_error = None

    # 2) Nominatim フォールバック
    try:
        params = {
            "q": address,
            "format": "json",
            "limit": 1,
            "accept-language": "ja",
        }
        headers = {"User-Agent": _USER_AGENT}
        resp = requests.get(
            _NOMINATIM_URL,
            params=params,
            headers=headers,
            timeout=_DEFAULT_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, list) and len(data) > 0:
            top = data[0]
            return {
                "lat": float(top["lat"]),
                "lng": float(top["lon"]),
                "formatted_address": top.get("display_name", address),
                "source": "nominatim",
                "error": None,
            }
        else:
            error_parts = []
            if google_error:
                error_parts.append(google_error)
            error_parts.append("Nominatim returned no results")
            result["error"] = " / ".join(error_parts)
            return result
    except Exception as e:
        error_parts = []
        if google_error:
            error_parts.append(google_error)
        error_parts.append(f"Nominatim exception: {e}")
        result["error"] = " / ".join(error_parts)
        return result


def fetch_satellite_image(
    lat: float,
    lng: float,
    zoom: int = 20,
    size: Tuple[int, int] = (640, 640),
) -> dict:
    """緯度経度から衛星画像を取得する。

    Args:
        lat: 緯度
        lng: 経度
        zoom: ズームレベル（20で約 0.15m/px）
        size: 画像サイズ (幅, 高さ)。Google Static Maps のとき有効

    Returns:
        {
            "image_bytes": bytes,
            "media_type": "image/png" | "image/jpeg",
            "source": "google" | "esri" | "error",
            "scale_meter_per_pixel": float,
            "error": str | None,
        }
    """
    result = {
        "image_bytes": None,
        "media_type": "image/png",
        "source": "error",
        "scale_meter_per_pixel": _zoom_to_meter_per_pixel(lat, zoom),
        "error": None,
    }

    google_error: Optional[str] = None

    # 1) Google Static Maps API
    api_key = _get_google_api_key()
    if api_key:
        try:
            w, h = size
            # Google Static Maps は最大640x640（無料枠）
            w = max(1, min(640, int(w)))
            h = max(1, min(640, int(h)))
            params = {
                "center": f"{lat},{lng}",
                "zoom": zoom,
                "size": f"{w}x{h}",
                "maptype": "satellite",
                "key": api_key,
                "format": "png",
            }
            resp = requests.get(
                _GOOGLE_STATICMAP_URL, params=params, timeout=_DEFAULT_TIMEOUT
            )
            resp.raise_for_status()
            content_type = resp.headers.get("Content-Type", "image/png")
            if content_type.startswith("image/"):
                return {
                    "image_bytes": resp.content,
                    "media_type": content_type.split(";")[0].strip(),
                    "source": "google",
                    "scale_meter_per_pixel": _zoom_to_meter_per_pixel(lat, zoom),
                    "error": None,
                }
            else:
                google_error = (
                    f"Google Static Maps returned non-image content-type={content_type}"
                )
        except Exception as e:
            google_error = f"Google Static Maps exception: {e}"

    # 2) Esri World Imagery タイル合成（無料）
    try:
        png_bytes = _compose_tiles_from_esri(lat, lng, zoom)
        return {
            "image_bytes": png_bytes,
            "media_type": "image/png",
            "source": "esri",
            "scale_meter_per_pixel": _zoom_to_meter_per_pixel(lat, zoom),
            "error": None,
        }
    except Exception as e:
        error_parts = []
        if google_error:
            error_parts.append(google_error)
        error_parts.append(f"Esri compose exception: {e}")
        result["error"] = " / ".join(error_parts)
        return result


def get_roof_view(address: str, zoom: int = 20) -> dict:
    """住所から屋根の衛星画像を取得する（geocode + 衛星画像取得）。

    Args:
        address: 住所文字列
        zoom: ズームレベル（デフォルト20、約 0.15m/px）

    Returns:
        {
            "address": str,
            "lat": float | None,
            "lng": float | None,
            "image_bytes": bytes | None,
            "media_type": str,
            "scale_meter_per_pixel": float | None,
            "source": str,
            "error": str | None,
        }
    """
    out = {
        "address": address,
        "lat": None,
        "lng": None,
        "image_bytes": None,
        "media_type": "image/png",
        "scale_meter_per_pixel": None,
        "source": "error",
        "error": None,
    }

    geo = geocode_address(address)
    if geo.get("error") or geo.get("lat") is None:
        out["error"] = f"geocode failed: {geo.get('error')}"
        out["source"] = "error"
        return out

    out["lat"] = geo["lat"]
    out["lng"] = geo["lng"]
    out["address"] = geo.get("formatted_address") or address

    img = fetch_satellite_image(geo["lat"], geo["lng"], zoom=zoom)
    if img.get("error") or img.get("image_bytes") is None:
        out["error"] = f"fetch_satellite_image failed: {img.get('error')}"
        out["source"] = f"{geo['source']}+error"
        out["scale_meter_per_pixel"] = img.get("scale_meter_per_pixel")
        return out

    out["image_bytes"] = img["image_bytes"]
    out["media_type"] = img["media_type"]
    out["scale_meter_per_pixel"] = img["scale_meter_per_pixel"]
    out["source"] = f"{geo['source']}+{img['source']}"
    out["error"] = None
    return out


# ---------------------------------------------------------------------------
# 動作確認
# ---------------------------------------------------------------------------


if __name__ == "__main__":
    test_addresses = [
        "東京都新宿区西新宿2-8-1",         # 新宿都庁
        "神奈川県横須賀市三春町2-10",       # 株式会社サンエー本社
    ]

    print("=" * 70)
    print("satellite_fetcher.py 動作確認")
    print(
        f"  Google API key: {'あり' if _get_google_api_key() else 'なし（フォールバック動作）'}"
    )
    print("=" * 70)

    for addr in test_addresses:
        print(f"\n[Test] {addr}")
        view = get_roof_view(addr, zoom=19)
        if view["error"] is None and view["image_bytes"] is not None:
            print(f"  -> source: {view['source']}")
            print(f"  -> formatted: {view['address']}")
            print(f"  -> lat/lng: {view['lat']:.6f}, {view['lng']:.6f}")
            print(f"  -> image bytes: {len(view['image_bytes']):,} bytes")
            print(f"  -> media_type: {view['media_type']}")
            print(
                f"  -> scale: {view['scale_meter_per_pixel']:.4f} m/pixel"
            )
        else:
            print(f"  -> ERROR: {view['error']}")
            print(f"  -> source: {view['source']}")
            print(f"  -> lat/lng: {view['lat']}, {view['lng']}")
