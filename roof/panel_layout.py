"""太陽光パネル簡易レイアウト計算・図示モジュール

屋根の寸法（幅×奥行き m）と太陽光パネルの物理寸法（長辺×短辺 m）を
入力に、最適な配置を計算してSVG/PNG画像でレイアウト図を生成する。

使用例:
    >>> from roof.panel_layout import (
    ...     compute_panel_layout,
    ...     panel_dimensions_from_module,
    ...     render_layout_svg,
    ... )
    >>> long_m, short_m = panel_dimensions_from_module(
    ...     "Canadian Solar", "CS7L-MS", 660
    ... )
    >>> layout = compute_panel_layout(
    ...     roof_width_m=15.0, roof_depth_m=10.0,
    ...     panel_long_m=long_m, panel_short_m=short_m,
    ... )
    >>> print(f"{layout['panel_count']}枚配置可能、容量 "
    ...       f"{layout['panel_count']*0.660:.1f}kW")
    >>> svg = render_layout_svg(layout, label="○○様邸 屋根レイアウト")
    >>> with open("/tmp/layout.svg", "w") as f:
    ...     f.write(svg)

公開関数:
    - compute_panel_layout(...): 屋根に何枚パネルを配置できるか計算
    - panel_dimensions_from_module(...): 製品情報からパネル物理寸法を逆引き
    - render_layout_svg(...): SVG文字列のレイアウト図を生成
    - render_layout_png(...): PNGバイナリのレイアウト図を生成
"""

from __future__ import annotations

import io
import math
import os
from typing import Optional

# --- パネル寸法のカタログ読み込み ---------------------------------------------

_KNOWLEDGE_YAML = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "knowledge",
    "panel_dimensions.yaml",
)

_PANEL_CACHE: Optional[list] = None


def _load_panel_catalog() -> list:
    """panel_dimensions.yaml を読み込んでパネルカタログを返す。

    yaml が無い、またはファイルが無い場合は空リストを返す。
    """
    global _PANEL_CACHE
    if _PANEL_CACHE is not None:
        return _PANEL_CACHE
    try:
        import yaml  # type: ignore
    except Exception:
        _PANEL_CACHE = []
        return _PANEL_CACHE
    if not os.path.exists(_KNOWLEDGE_YAML):
        _PANEL_CACHE = []
        return _PANEL_CACHE
    try:
        with open(_KNOWLEDGE_YAML, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        panels = data.get("panels", []) or []
        _PANEL_CACHE = list(panels)
    except Exception:
        _PANEL_CACHE = []
    return _PANEL_CACHE


# --- 典型値テーブル ----------------------------------------------------------

# (出力Wの上限, 長辺m, 短辺m) のリスト
# しきい値は「以下」で判定する
_TYPICAL_DIMS = [
    (300, 1.65, 1.00),
    (400, 1.75, 1.05),
    (500, 1.95, 1.10),
    (600, 2.20, 1.13),
    (700, 2.38, 1.13),
]
_DEFAULT_DIMS = (2.0, 1.0)  # 660W近辺のフルサイズパネル想定


def _typical_dimensions(output_w: float) -> tuple[float, float]:
    """出力Wから典型寸法を推定する。"""
    try:
        w = float(output_w)
    except Exception:
        return _DEFAULT_DIMS
    if w <= 0:
        return _DEFAULT_DIMS
    # 200W未満は最小サイズ寄り
    if w < 200:
        return (1.65, 1.00)
    for upper, long_m, short_m in _TYPICAL_DIMS:
        if w <= upper:
            return (long_m, short_m)
    return _DEFAULT_DIMS


# --- 公開API: パネル寸法逆引き ----------------------------------------------


def panel_dimensions_from_module(
    maker: str, model: str, output_w: float
) -> tuple[float, float]:
    """製品情報からパネル物理寸法を逆引きする。

    1. knowledge/panel_dimensions.yaml をメーカー名・型式プレフィックスで
       検索する。
    2. マッチがあれば (long_m, short_m) を返す。
    3. マッチがなければ出力Wから典型値を推定する。
    4. 全て失敗すれば既定値 (2.0, 1.0) を返す。
    """
    maker_s = (maker or "").strip().lower()
    model_s = (model or "").strip().lower()
    catalog = _load_panel_catalog()

    # 1) メーカー一致 + 型式プレフィックス一致
    if maker_s and model_s:
        for entry in catalog:
            em = str(entry.get("maker", "")).strip().lower()
            ep = str(entry.get("model_prefix", "")).strip().lower()
            if not em or not ep:
                continue
            if em == maker_s and model_s.startswith(ep):
                long_m = float(entry.get("long_m", 0) or 0)
                short_m = float(entry.get("short_m", 0) or 0)
                if long_m > 0 and short_m > 0:
                    return (long_m, short_m)

    # 2) メーカー一致 + 出力W近接（±20W以内）
    if maker_s and output_w:
        try:
            target_w = float(output_w)
            best = None
            best_diff = None
            for entry in catalog:
                em = str(entry.get("maker", "")).strip().lower()
                if em != maker_s:
                    continue
                ew = float(entry.get("output_w", 0) or 0)
                diff = abs(ew - target_w)
                if diff <= 20 and (best_diff is None or diff < best_diff):
                    best = entry
                    best_diff = diff
            if best is not None:
                long_m = float(best.get("long_m", 0) or 0)
                short_m = float(best.get("short_m", 0) or 0)
                if long_m > 0 and short_m > 0:
                    return (long_m, short_m)
        except Exception:
            pass

    # 3) 出力Wから典型値推定
    if output_w:
        return _typical_dimensions(output_w)

    return _DEFAULT_DIMS


# --- 公開API: レイアウト計算 ------------------------------------------------


def _compute_one_orientation(
    roof_w: float,
    roof_d: float,
    panel_w: float,
    panel_h: float,
    margin: float,
    gap: float,
) -> tuple[int, int, list]:
    """指定向きのパネル枚数と座標を計算。

    panel_w: パネル横幅（屋根幅方向の寸法）
    panel_h: パネル奥行き（屋根奥行き方向の寸法）
    """
    avail_w = roof_w - 2 * margin
    avail_d = roof_d - 2 * margin
    if avail_w <= 0 or avail_d <= 0 or panel_w <= 0 or panel_h <= 0:
        return 0, 0, []

    # gap 込みのピッチで割る。ただし最後のパネルの後ろにgapは要らない
    # n*panel + (n-1)*gap <= avail
    # n <= (avail + gap) / (panel + gap)
    cols = int(math.floor((avail_w + gap) / (panel_w + gap))) if (panel_w + gap) > 0 else 0
    rows = int(math.floor((avail_d + gap) / (panel_h + gap))) if (panel_h + gap) > 0 else 0
    cols = max(0, cols)
    rows = max(0, rows)

    positions = []
    if rows > 0 and cols > 0:
        # 余白を左右に均等配分（中央寄せ）
        used_w = cols * panel_w + (cols - 1) * gap
        used_d = rows * panel_h + (rows - 1) * gap
        offset_x = margin + max(0.0, (avail_w - used_w) / 2.0)
        offset_y = margin + max(0.0, (avail_d - used_d) / 2.0)
        for r in range(rows):
            for c in range(cols):
                x = offset_x + c * (panel_w + gap)
                y = offset_y + r * (panel_h + gap)
                positions.append({"x": x, "y": y, "w": panel_w, "h": panel_h})
    return rows, cols, positions


def compute_panel_layout(
    roof_width_m: float,
    roof_depth_m: float,
    panel_long_m: float,
    panel_short_m: float,
    edge_margin_m: float = 0.5,
    gap_m: float = 0.02,
    orientation: str = "auto",
) -> dict:
    """屋根に何枚パネルを配置できるか計算する。

    Args:
        roof_width_m: 屋根の幅（東西方向）[m]
        roof_depth_m: 屋根の奥行き（南北方向）[m]
        panel_long_m: パネルの長辺 [m]
        panel_short_m: パネルの短辺 [m]
        edge_margin_m: 屋根エッジからのマージン [m]
        gap_m: パネル間ギャップ [m]
        orientation: "portrait"=パネル長辺を屋根奥行きと平行
                     "landscape"=パネル長辺を屋根幅と平行
                     "auto"=両方計算して枚数の多い方を採用

    Returns:
        dict: {orientation, panel_count, rows, cols, occupied_area_sqm,
               roof_area_sqm, fill_ratio, positions, margin_m, gap_m,
               roof_width_m, roof_depth_m, panel_long_m, panel_short_m}
    """
    # 入力ガード
    try:
        rw = float(roof_width_m)
        rd = float(roof_depth_m)
        pl = float(panel_long_m)
        ps = float(panel_short_m)
        margin = float(edge_margin_m)
        gap = float(gap_m)
    except Exception:
        rw = rd = pl = ps = margin = gap = 0.0

    roof_area = max(0.0, rw) * max(0.0, rd)

    def _empty(orient: str) -> dict:
        return {
            "orientation": orient,
            "panel_count": 0,
            "rows": 0,
            "cols": 0,
            "occupied_area_sqm": 0.0,
            "roof_area_sqm": roof_area,
            "fill_ratio": 0.0,
            "positions": [],
            "margin_m": margin,
            "gap_m": gap,
            "roof_width_m": rw,
            "roof_depth_m": rd,
            "panel_long_m": pl,
            "panel_short_m": ps,
        }

    if rw <= 0 or rd <= 0 or pl <= 0 or ps <= 0:
        return _empty(orientation if orientation in ("portrait", "landscape") else "portrait")

    # 長辺/短辺の正規化
    long_m = max(pl, ps)
    short_m = min(pl, ps)

    # portrait: パネル長辺が屋根奥行き(縦)方向 → 横=short, 縦=long
    # landscape: パネル長辺が屋根幅(横)方向 → 横=long, 縦=short
    rows_p, cols_p, pos_p = _compute_one_orientation(
        rw, rd, short_m, long_m, margin, gap
    )
    rows_l, cols_l, pos_l = _compute_one_orientation(
        rw, rd, long_m, short_m, margin, gap
    )
    count_p = rows_p * cols_p
    count_l = rows_l * cols_l

    if orientation == "portrait":
        chosen = ("portrait", rows_p, cols_p, pos_p, short_m, long_m)
    elif orientation == "landscape":
        chosen = ("landscape", rows_l, cols_l, pos_l, long_m, short_m)
    else:  # auto
        if count_l > count_p:
            chosen = ("landscape", rows_l, cols_l, pos_l, long_m, short_m)
        else:
            chosen = ("portrait", rows_p, cols_p, pos_p, short_m, long_m)

    orient, rows, cols, positions, panel_w, panel_h = chosen
    panel_count = rows * cols
    panel_area = long_m * short_m
    occupied = panel_count * panel_area
    fill_ratio = (occupied / roof_area) if roof_area > 0 else 0.0

    return {
        "orientation": orient,
        "panel_count": panel_count,
        "rows": rows,
        "cols": cols,
        "occupied_area_sqm": occupied,
        "roof_area_sqm": roof_area,
        "fill_ratio": fill_ratio,
        "positions": positions,
        "margin_m": margin,
        "gap_m": gap,
        "roof_width_m": rw,
        "roof_depth_m": rd,
        "panel_long_m": long_m,
        "panel_short_m": short_m,
        "panel_w_m": panel_w,
        "panel_h_m": panel_h,
    }


# --- 公開API: SVGレンダラ ---------------------------------------------------

# 配色（CLAUDE.md トンマナ準拠）
_COLOR_BG = "#ffffff"
_COLOR_SUBBG = "#f5f7fa"
_COLOR_TEXT = "#1a1a2e"
_COLOR_SUBTEXT = "#4a5568"
_COLOR_ACCENT = "#1e3a5f"
_COLOR_BORDER = "#e2e8f0"
_COLOR_PANEL = "#1E3A5F"
_COLOR_PANEL_BORDER = "#ffffff"


def _kw_from_count(panel_count: int, layout: dict) -> float:
    """パネル枚数から容量[kW]を概算。1枚あたりの出力はpanel_long×short比例の簡易推定。"""
    # ざっくり 1枚=0.45kW ベース。長辺/短辺の積から類推
    long_m = layout.get("panel_long_m", 0)
    short_m = layout.get("panel_short_m", 0)
    area = (long_m or 0) * (short_m or 0)
    if area <= 0:
        return panel_count * 0.45
    # 出力密度 約 200W/m² 仮定
    return panel_count * area * 0.20


def render_layout_svg(layout: dict, label: str = "") -> str:
    """レイアウト計算結果をSVG文字列で生成する。

    - 屋根輪郭: 黒1px ボーダー
    - マージン枠: 点線
    - 各パネル: 濃紺塗り + 白0.5px ボーダー
    - タイトル: 上部中央
    - 統計フッター: 下部中央
    - 凡例（N↑）: 左下
    """
    rw = float(layout.get("roof_width_m", 0) or 0)
    rd = float(layout.get("roof_depth_m", 0) or 0)
    margin = float(layout.get("margin_m", 0) or 0)
    positions = layout.get("positions", []) or []
    rows = int(layout.get("rows", 0) or 0)
    cols = int(layout.get("cols", 0) or 0)
    panel_count = int(layout.get("panel_count", 0) or 0)
    fill = float(layout.get("fill_ratio", 0) or 0)
    kw = _kw_from_count(panel_count, layout)

    # スケール: 最大幅 800px
    max_canvas_w = 800.0
    title_h = 60.0
    footer_h = 50.0
    side_pad = 30.0
    legend_h = 30.0

    if rw <= 0 or rd <= 0:
        # 空フォールバック
        return (
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 400 200" '
            f'width="400" height="200" style="background:{_COLOR_BG};">'
            f'<text x="200" y="100" text-anchor="middle" '
            f'fill="{_COLOR_SUBTEXT}" font-family="Noto Sans JP, sans-serif" '
            f'font-size="14">屋根サイズが指定されていません</text>'
            f'</svg>'
        )

    # 屋根の表示エリア
    inner_w = max_canvas_w - 2 * side_pad
    scale = inner_w / rw  # px per meter
    inner_h = rd * scale
    canvas_w = max_canvas_w
    canvas_h = title_h + inner_h + footer_h + legend_h

    roof_x = side_pad
    roof_y = title_h

    parts: list[str] = []
    parts.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {canvas_w:.1f} {canvas_h:.1f}" '
        f'width="{canvas_w:.0f}" height="{canvas_h:.0f}" '
        f'style="background:{_COLOR_BG};font-family:\'Noto Sans JP\',sans-serif;">'
    )

    # タイトル
    if label:
        parts.append(
            f'<text x="{canvas_w/2:.1f}" y="28" text-anchor="middle" '
            f'fill="{_COLOR_ACCENT}" font-size="18" font-weight="700">'
            f'{_escape(label)}</text>'
        )
    parts.append(
        f'<text x="{canvas_w/2:.1f}" y="48" text-anchor="middle" '
        f'fill="{_COLOR_SUBTEXT}" font-size="11">'
        f'屋根 {rw:.2f}m × {rd:.2f}m / マージン {margin:.2f}m / '
        f'配置: {layout.get("orientation", "")}</text>'
    )

    # 屋根背景（薄グレー）
    parts.append(
        f'<rect x="{roof_x:.2f}" y="{roof_y:.2f}" '
        f'width="{rw*scale:.2f}" height="{rd*scale:.2f}" '
        f'fill="{_COLOR_SUBBG}" stroke="{_COLOR_TEXT}" stroke-width="1.2"/>'
    )

    # マージン枠（点線）
    if margin > 0:
        mx = roof_x + margin * scale
        my = roof_y + margin * scale
        mw = (rw - 2 * margin) * scale
        mh = (rd - 2 * margin) * scale
        if mw > 0 and mh > 0:
            parts.append(
                f'<rect x="{mx:.2f}" y="{my:.2f}" '
                f'width="{mw:.2f}" height="{mh:.2f}" '
                f'fill="none" stroke="{_COLOR_SUBTEXT}" stroke-width="0.8" '
                f'stroke-dasharray="4,3"/>'
            )

    # パネル
    for p in positions:
        px = roof_x + float(p.get("x", 0)) * scale
        py = roof_y + float(p.get("y", 0)) * scale
        pw = float(p.get("w", 0)) * scale
        ph = float(p.get("h", 0)) * scale
        parts.append(
            f'<rect x="{px:.2f}" y="{py:.2f}" width="{pw:.2f}" height="{ph:.2f}" '
            f'fill="{_COLOR_PANEL}" stroke="{_COLOR_PANEL_BORDER}" stroke-width="0.5"/>'
        )

    # 寸法ラベル（屋根サイズを小さく表示）
    parts.append(
        f'<text x="{roof_x + rw*scale/2:.2f}" y="{roof_y + rd*scale + 14:.2f}" '
        f'text-anchor="middle" fill="{_COLOR_SUBTEXT}" font-size="10">'
        f'{rw:.2f} m</text>'
    )
    parts.append(
        f'<text x="{roof_x - 8:.2f}" y="{roof_y + rd*scale/2:.2f}" '
        f'text-anchor="end" fill="{_COLOR_SUBTEXT}" font-size="10" '
        f'transform="rotate(-90 {roof_x - 8:.2f} {roof_y + rd*scale/2:.2f})">'
        f'{rd:.2f} m</text>'
    )

    # 凡例（N↑）左下
    legend_x = side_pad
    legend_y = canvas_h - legend_h + 8
    parts.append(
        f'<g transform="translate({legend_x:.1f},{legend_y:.1f})">'
        f'<line x1="6" y1="14" x2="6" y2="2" stroke="{_COLOR_ACCENT}" stroke-width="1.5"/>'
        f'<polygon points="3,4 6,0 9,4" fill="{_COLOR_ACCENT}"/>'
        f'<text x="14" y="11" fill="{_COLOR_ACCENT}" font-size="11" font-weight="700">N</text>'
        f'</g>'
    )

    # フッター統計
    footer_text = (
        f'{rows}行 × {cols}列 = {panel_count}枚 / '
        f'容量 {kw:.1f} kW / 充填率 {fill*100:.0f}%'
    )
    parts.append(
        f'<text x="{canvas_w/2:.1f}" y="{canvas_h - legend_h + 16:.1f}" '
        f'text-anchor="middle" fill="{_COLOR_TEXT}" font-size="13" font-weight="700">'
        f'{footer_text}</text>'
    )

    parts.append('</svg>')
    return "".join(parts)


def _escape(s: str) -> str:
    return (
        str(s)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


# --- 公開API: PNGレンダラ ---------------------------------------------------


def render_layout_png(layout: dict, label: str = "", dpi: int = 150) -> bytes:
    """レイアウトをPNGバイトとして返す。

    優先順位:
    1. matplotlib があれば使う（高品質）
    2. cairosvg があれば SVG → PNG 変換
    3. PIL があれば簡易描画
    4. 全部無ければ SVG をそのままバイト化（拡張子だけPNGの代替）

    Returns:
        bytes: PNG画像のバイナリ
    """
    # 1) matplotlib
    try:
        import matplotlib  # type: ignore

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt  # type: ignore
        from matplotlib.patches import Rectangle  # type: ignore

        rw = float(layout.get("roof_width_m", 0) or 0)
        rd = float(layout.get("roof_depth_m", 0) or 0)
        margin = float(layout.get("margin_m", 0) or 0)
        positions = layout.get("positions", []) or []
        rows = int(layout.get("rows", 0) or 0)
        cols = int(layout.get("cols", 0) or 0)
        panel_count = int(layout.get("panel_count", 0) or 0)
        fill = float(layout.get("fill_ratio", 0) or 0)
        kw = _kw_from_count(panel_count, layout)

        # アスペクト比から figsize を決定
        if rw <= 0 or rd <= 0:
            fig, ax = plt.subplots(figsize=(6, 3), dpi=dpi)
            ax.text(0.5, 0.5, "屋根サイズが指定されていません",
                    ha="center", va="center", color=_COLOR_SUBTEXT)
            ax.axis("off")
        else:
            fig_w = 8.0
            fig_h = max(3.0, fig_w * (rd / rw) + 1.5)
            fig, ax = plt.subplots(figsize=(fig_w, fig_h), dpi=dpi)
            fig.patch.set_facecolor(_COLOR_BG)
            ax.set_facecolor(_COLOR_BG)

            # 屋根
            ax.add_patch(Rectangle((0, 0), rw, rd,
                                   facecolor=_COLOR_SUBBG,
                                   edgecolor=_COLOR_TEXT, linewidth=1.2))
            # マージン
            if margin > 0 and rw - 2 * margin > 0 and rd - 2 * margin > 0:
                ax.add_patch(Rectangle(
                    (margin, margin), rw - 2 * margin, rd - 2 * margin,
                    facecolor="none", edgecolor=_COLOR_SUBTEXT,
                    linewidth=0.8, linestyle="--"))
            # パネル
            for p in positions:
                ax.add_patch(Rectangle(
                    (float(p.get("x", 0)), float(p.get("y", 0))),
                    float(p.get("w", 0)), float(p.get("h", 0)),
                    facecolor=_COLOR_PANEL,
                    edgecolor=_COLOR_PANEL_BORDER, linewidth=0.4))

            ax.set_xlim(-0.3, rw + 0.3)
            # 北を上に: y軸を反転（左上原点）
            ax.set_ylim(rd + 0.3, -0.3)
            ax.set_aspect("equal")
            ax.set_xlabel(f"{rw:.2f} m", color=_COLOR_SUBTEXT, fontsize=9)
            ax.set_ylabel(f"{rd:.2f} m", color=_COLOR_SUBTEXT, fontsize=9)
            ax.tick_params(colors=_COLOR_SUBTEXT, labelsize=8)
            for s in ax.spines.values():
                s.set_color(_COLOR_BORDER)

            title_lines = []
            if label:
                title_lines.append(label)
            title_lines.append(
                f'{rows}行 × {cols}列 = {panel_count}枚 / '
                f'容量 {kw:.1f} kW / 充填率 {fill*100:.0f}%'
            )
            ax.set_title("\n".join(title_lines),
                         color=_COLOR_ACCENT, fontsize=12, fontweight="bold")

            # 凡例 N↑
            ax.annotate(
                "N", xy=(0.0, 0.0), xytext=(0.0, 0.6),
                color=_COLOR_ACCENT, fontsize=10, fontweight="bold",
                ha="center", va="bottom",
                arrowprops=dict(arrowstyle="->", color=_COLOR_ACCENT, lw=1.2),
            )

        buf = io.BytesIO()
        fig.tight_layout()
        fig.savefig(buf, format="png", dpi=dpi,
                    facecolor=fig.get_facecolor())
        plt.close(fig)
        return buf.getvalue()
    except Exception:
        pass

    # 2) cairosvg
    try:
        import cairosvg  # type: ignore

        svg = render_layout_svg(layout, label=label)
        return cairosvg.svg2png(bytestring=svg.encode("utf-8"), dpi=dpi)
    except Exception:
        pass

    # 3) PIL
    try:
        from PIL import Image, ImageDraw  # type: ignore

        rw = float(layout.get("roof_width_m", 0) or 0)
        rd = float(layout.get("roof_depth_m", 0) or 0)
        positions = layout.get("positions", []) or []

        max_w = 800
        if rw <= 0 or rd <= 0:
            img = Image.new("RGB", (400, 200), _COLOR_BG)
            draw = ImageDraw.Draw(img)
            draw.text((100, 90), "no roof size", fill=_COLOR_SUBTEXT)
        else:
            scale = (max_w - 60) / rw
            img_w = max_w
            img_h = int(rd * scale + 100)
            img = Image.new("RGB", (img_w, img_h), _COLOR_BG)
            draw = ImageDraw.Draw(img)
            ox, oy = 30, 60
            draw.rectangle(
                [ox, oy, ox + rw * scale, oy + rd * scale],
                fill=_COLOR_SUBBG, outline=_COLOR_TEXT, width=2,
            )
            for p in positions:
                x = ox + float(p.get("x", 0)) * scale
                y = oy + float(p.get("y", 0)) * scale
                w = float(p.get("w", 0)) * scale
                h = float(p.get("h", 0)) * scale
                draw.rectangle([x, y, x + w, y + h],
                               fill=_COLOR_PANEL, outline=_COLOR_PANEL_BORDER)
            if label:
                draw.text((10, 10), label, fill=_COLOR_ACCENT)
            panel_count = int(layout.get("panel_count", 0) or 0)
            kw = _kw_from_count(panel_count, layout)
            fill = float(layout.get("fill_ratio", 0) or 0)
            footer = f"{panel_count}枚 / {kw:.1f}kW / {fill*100:.0f}%"
            draw.text((10, img_h - 20), footer, fill=_COLOR_TEXT)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()
    except Exception:
        pass

    # 4) 最終フォールバック: SVGバイト
    return render_layout_svg(layout, label=label).encode("utf-8")


# --- 動作確認 ---------------------------------------------------------------


if __name__ == "__main__":
    import os as _os

    print("=" * 60)
    print("Test 1: 屋根 15m×10m, Canadian Solar CS7L 660W パネル, auto")
    print("=" * 60)
    long_m, short_m = panel_dimensions_from_module(
        "Canadian Solar", "CS7L-MS", 660
    )
    print(f"  パネル寸法: {long_m:.3f}m × {short_m:.3f}m")
    layout = compute_panel_layout(
        roof_width_m=15.0,
        roof_depth_m=10.0,
        panel_long_m=long_m,
        panel_short_m=short_m,
        orientation="auto",
    )
    print(f"  配置: {layout['orientation']}")
    print(f"  {layout['rows']}行 × {layout['cols']}列 = {layout['panel_count']}枚")
    print(f"  屋根面積: {layout['roof_area_sqm']:.2f}m²")
    print(f"  パネル占有: {layout['occupied_area_sqm']:.2f}m²")
    print(f"  充填率: {layout['fill_ratio']*100:.1f}%")
    print(f"  容量: {layout['panel_count']*0.660:.2f} kW")

    out_svg = "/tmp/test_layout.svg"
    svg = render_layout_svg(
        layout, label="テスト邸 屋根レイアウト (Canadian Solar 660W)"
    )
    with open(out_svg, "w", encoding="utf-8") as f:
        f.write(svg)
    print(f"  SVG出力: {out_svg} ({_os.path.getsize(out_svg)} bytes)")

    print()
    print("=" * 60)
    print("Test 2: 屋根 8m×5m, シャープ NU-580 580W パネル")
    print("=" * 60)
    long_m, short_m = panel_dimensions_from_module(
        "シャープ", "NU-580", 580
    )
    print(f"  パネル寸法: {long_m:.3f}m × {short_m:.3f}m")

    for orient in ("portrait", "landscape", "auto"):
        layout = compute_panel_layout(
            roof_width_m=8.0,
            roof_depth_m=5.0,
            panel_long_m=long_m,
            panel_short_m=short_m,
            orientation=orient,
        )
        print(
            f"  {orient:>9s}: {layout['rows']}行×{layout['cols']}列 = "
            f"{layout['panel_count']}枚 / "
            f"充填率 {layout['fill_ratio']*100:.1f}%"
        )

    print()
    print("=" * 60)
    print("Test 3: 異常値ガード")
    print("=" * 60)
    layout = compute_panel_layout(0, 10, 2, 1)
    print(f"  roof_width=0 → panel_count={layout['panel_count']}")
    layout = compute_panel_layout(10, 10, 0, 1)
    print(f"  panel_long=0 → panel_count={layout['panel_count']}")

    print()
    print("=" * 60)
    print("Test 4: 未知メーカーのフォールバック")
    print("=" * 60)
    long_m, short_m = panel_dimensions_from_module("UnknownCo", "XX-450", 450)
    print(f"  450W 未知パネル → ({long_m:.2f}, {short_m:.2f})")
    long_m, short_m = panel_dimensions_from_module("", "", 0)
    print(f"  全空 → ({long_m:.2f}, {short_m:.2f})")
