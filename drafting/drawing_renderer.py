"""CAD図面レンダラ（株式会社サンエー標準テンプレート）

DraftingSpec（パネル座標まで確定済み）を入力に、サンエーの実完成製図に
見た目を寄せた太陽光配置図／ストリングス図を PNG / PDF で描画する。

レイアウト方針（紙は常に横向き。A4=297x210mm / A3=420x297mm）:
    ┌────────────────────────────────────────────────────────┐
    │ [SANEIワードマーク]                         [方位記号N]   │
    │                                             ┌──────────┐ │
    │   メイン作図域（屋根プラン・パネル・寸法）    │情報ボックス│ │
    │   左~62%                                     │(系統表)   │ │
    │                                             │架台断面図 │ │
    │                                             │パネル詳細 │ │
    │                                             └──────────┘ │
    │ ┌─────────────────────────[タイトルブロック]───────────┐ │
    └────────────────────────────────────────────────────────┘

設計判断:
- 図面はすべて Figure 全体を 0..1 の正規化座標で扱い、紙サイズ比で Figure を作る。
- メイン作図域内の屋根は mm 座標を等比スケールで図上に写像（アスペクト比保持）。
- matplotlib(Agg) のみ使用。日本語は同梱 NotoSansJP を font_manager に登録して描画。
"""

from __future__ import annotations

import io
import math
from typing import Optional

import matplotlib

matplotlib.use("Agg")  # GUI 非依存（ヘッドレス描画）
import matplotlib.pyplot as plt
from matplotlib import font_manager
from matplotlib.patches import Rectangle, Polygon, Circle
from matplotlib.lines import Line2D

from drafting.models import (
    DraftingSpec, RoofFace, PanelRect,
    DrawingType, RoofType, Orientation, MountType,
)

# =============================================================
# 配色（CLAUDE.md トンマナ準拠）
# =============================================================

COL_BLACK = "#1a1a2e"      # 図枠・外形・罫線
COL_NAVY = "#1e3a5f"       # 情報ボックス見出し・濃紺アクセント
COL_RED = "#c53030"        # 寸法線・寸法数値（住宅）
COL_GREEN = "#276749"      # 寸法線（法人の屋根全幅など）
COL_MAGENTA = "#FF00FF"    # パネル枠（配置図）
COL_GRAY = "#cccccc"       # 屋根ハッチ・薄罫
COL_HATCH = "#b0b0b0"      # 屋根ハッチ線（やや濃いめ）
COL_WHITE = "#ffffff"

# ストリングス図のストリング色巡回（マゼンタ/赤/シアン/青/緑系）
STRING_COLORS = [
    "#FF00FF",  # マゼンタ
    "#c53030",  # 赤
    "#00B5C8",  # シアン
    "#1e6fd9",  # 青
    "#2f9e44",  # 緑
    "#9c36b5",  # 紫
    "#e8590c",  # オレンジ
]

# 会社情報（住所・TEL/FAX は実サンプル準拠で上書き）
COMPANY = {
    "name": "株式会社　サンエー",
    "slogan": "未来の当たり前を、いちはやく",
    "postal": "〒238-0014",
    "address": "神奈川県横須賀市三春町4-1-10",
    "tel": "TEL：046-828-3351",
    "fax": "FAX：046-828-3352",
}

# 縮尺の選択肢（実スケールから近い分母を選ぶ）
_SCALE_DENOMS = (60, 75, 100, 150, 200, 300)

# フォント登録（モジュール読み込み時に1度だけ）
_FONT_DIR = (
    "/Users/takaishouhei/Claude案件/株式会社サンエー/見積もり作成AI/assets/fonts"
)
_FP_REGULAR: Optional[font_manager.FontProperties] = None
_FP_BOLD: Optional[font_manager.FontProperties] = None


def _ensure_fonts() -> None:
    """NotoSansJP を font_manager に登録し、FontProperties を準備する。

    フォントファイルが見つからない場合でも例外で落とさず、
    matplotlib 既定フォントへフォールバックする（英数字は描ける）。
    """
    global _FP_REGULAR, _FP_BOLD
    if _FP_REGULAR is not None:
        return
    import os

    reg = os.path.join(_FONT_DIR, "NotoSansJP-Regular.ttf")
    bold = os.path.join(_FONT_DIR, "NotoSansJP-Bold.ttf")
    try:
        if os.path.exists(reg):
            font_manager.fontManager.addfont(reg)
            _FP_REGULAR = font_manager.FontProperties(fname=reg)
        if os.path.exists(bold):
            font_manager.fontManager.addfont(bold)
            _FP_BOLD = font_manager.FontProperties(fname=bold)
    except Exception:
        # フォント登録に失敗しても描画自体は続行する
        pass
    if _FP_REGULAR is None:
        _FP_REGULAR = font_manager.FontProperties()
    if _FP_BOLD is None:
        _FP_BOLD = _FP_REGULAR


# =============================================================
# 公開関数
# =============================================================

def render_drawing(spec: DraftingSpec, *, dpi: int = 150) -> dict:
    """製図を PNG / PDF の両方でレンダリングして返す。

    Args:
        spec: 描画対象の仕様（roof_faces[].panels が配置済み前提。
              未配置なら内部で簡易グリッドにフォールバックする）。
        dpi: PNG のラスタ解像度（PDF はベクタなので無関係）。

    Returns:
        {"png_bytes": bytes, "pdf_bytes": bytes}
    """
    if spec is None:
        raise ValueError("spec が None です")
    png = render_drawing_png(spec, dpi=dpi)
    pdf = render_drawing_pdf(spec)
    return {"png_bytes": png, "pdf_bytes": pdf}


def render_drawing_png(spec: DraftingSpec, dpi: int = 150) -> bytes:
    """PNG バイト列を返す。"""
    fig = _build_figure(spec)
    buf = io.BytesIO()
    try:
        fig.savefig(buf, format="png", dpi=dpi, facecolor=COL_WHITE)
    finally:
        plt.close(fig)
    return buf.getvalue()


def render_drawing_pdf(spec: DraftingSpec) -> bytes:
    """PDF バイト列を返す（ベクタ出力）。"""
    fig = _build_figure(spec)
    buf = io.BytesIO()
    try:
        fig.savefig(buf, format="pdf", facecolor=COL_WHITE)
    finally:
        plt.close(fig)
    return buf.getvalue()


# =============================================================
# Figure 組み立て
# =============================================================

def _paper_size_inch(paper: str) -> tuple:
    """紙サイズ（横向き）を inch で返す。"""
    if (paper or "A4").upper() == "A3":
        w_mm, h_mm = 420.0, 297.0
    else:
        w_mm, h_mm = 297.0, 210.0
    return (w_mm / 25.4, h_mm / 25.4)


def _build_figure(spec: DraftingSpec):
    """1 枚の Figure を組み立てて返す。

    Figure 全体を 1 枚の Axes（0..1 正規化座標, アスペクト比は紙比）として扱い、
    各領域をその座標系に描く。これにより mm→図上の写像を自前で管理できる。
    """
    _ensure_fonts()
    # 配置前なら簡易グリッドで埋める（本番は layout_engine が埋める）
    _ensure_panels(spec)

    w_in, h_in = _paper_size_inch(spec.paper)
    fig = plt.figure(figsize=(w_in, h_in), facecolor=COL_WHITE)
    ax = fig.add_axes([0.0, 0.0, 1.0, 1.0])
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.set_aspect(h_in / w_in)  # x:y を紙の実比に合わせる（円が円に見える）
    ax.axis("off")

    # 図全体のアスペクト（y を x に対し縮める係数）。寸法線の見栄え調整に使う。
    aspect = h_in / w_in

    # 1) 外枠
    _draw_border(ax)

    # 2) 左上 SANEI ワードマーク
    _draw_wordmark(ax)

    # 4) 右上 方位記号
    _draw_compass(ax, aspect)

    # 下部タイトルブロック領域（先に確保して残りをメイン/右カラムに割る）
    tb_top = 0.105  # タイトルブロックの上端 y

    # 3) メイン作図域（左 ~62%）
    main_box = (0.045, tb_top + 0.02, 0.575, 0.78)  # (x0,y0,w,h)
    _draw_main_plan(ax, spec, main_box, aspect)

    # 5) 右カラム（情報ボックス・系統表・架台断面・パネル詳細）
    right_x0 = 0.655
    right_w = 0.30
    _draw_right_column(ax, spec, right_x0, right_w, tb_top, aspect)

    # 6) 下部タイトルブロック
    _draw_title_block(ax, spec, tb_top, aspect)

    return fig


# =============================================================
# パネル配置フォールバック（自己テスト・未配置時用）
# =============================================================

def _ensure_panels(spec: DraftingSpec) -> None:
    """panels 未配置の屋根面に簡易グリッドを敷く（本番は layout_engine 担当）。"""
    for face in spec.roof_faces:
        if face.panels:
            continue
        _fill_simple_grid(spec, face)


def _fill_simple_grid(spec: DraftingSpec, face: RoofFace) -> None:
    """1 屋根面に target_panel_count を満たす矩形グリッドを敷く。

    向き(orientation)に応じてパネル寸法を決め、屋根外接矩形からマージンを引いた
    領域に行×列で並べる。あくまでテスト用の簡易配置。
    """
    p = spec.panel
    long_mm = p.long_mm or 1762.0
    short_mm = p.short_mm or 1134.0
    gap_long = p.gap_long_mm or 25.0    # 列方向（パネル長辺が並ぶ方向）の隙間
    gap_short = p.gap_short_mm or 10.0  # 行方向の隙間

    # 向き決定: landscape=長辺が幅方向(w), portrait=短辺が幅方向(w)
    orient = face.orientation
    if orient == Orientation.AUTO:
        orient = Orientation.LANDSCAPE
    if orient == Orientation.LANDSCAPE:
        pw, ph = long_mm, short_mm
    else:
        pw, ph = short_mm, long_mm

    bw, bh = face.bounds_mm()
    if bw <= 0 or bh <= 0:
        bw, bh = 10000.0, 6000.0

    target = int(face.target_panel_count or 0)
    if target <= 0:
        target = 10

    # マージンは「幅方向のみ」優先で適用。高さ方向は target 枚を収めるため
    # 必要に応じて圧縮する（実サンプルでは行方向マージンが小さいことが多い）。
    margin = face.margin_mm or 0.0
    avail_w = max(bw - 2 * margin, pw)

    # 幅に入る最大列数
    max_cols = max(1, int((avail_w + gap_short) // (pw + gap_short)))

    # target を満たす rows×cols を、なるべく行を詰めて決める。
    # まず幅いっぱいの列数を仮置きし、必要行数を算出。
    cols = min(max_cols, target)
    rows = max(1, math.ceil(target / cols))

    # 高さに rows 行が入るか確認。入らなければ「上下マージンを縮めてでも」収める。
    needed_h = rows * ph + (rows - 1) * gap_long
    if needed_h > bh:
        # 高さに入る最大行数まで減らす（マージンほぼ0で評価）
        max_rows = max(1, int((bh + gap_long) // (ph + gap_long)))
        rows = min(rows, max_rows)
        cols = min(max_cols, math.ceil(target / rows))

    # 隙間: 横方向(=列間)は gap_short、縦方向(=行間)は gap_long を使う
    grid_w = cols * pw + (cols - 1) * gap_short
    grid_h = rows * ph + (rows - 1) * gap_long
    # 屋根中央に寄せる（左右・上下とも中央配置で安定させる）
    x0 = (bw - grid_w) / 2.0
    y0 = (bh - grid_h) / 2.0

    panels = []
    placed = 0
    for r in range(rows):
        for c in range(cols):
            if placed >= target:
                break
            x = x0 + c * (pw + gap_short)
            y = y0 + r * (ph + gap_long)
            # ストリングID: 行をまたいだ連番をざっくり割当（テスト用）
            sid = f"{(placed // max(cols, 1)) + 1}"
            panels.append(PanelRect(
                x_mm=x, y_mm=y, w_mm=pw, h_mm=ph,
                orientation=orient, string_id=sid,
            ))
            placed += 1
    face.panels = panels
    face.rows = rows
    face.cols = cols
    face.panel_count = len(panels)


# =============================================================
# 描画ヘルパ（共通）
# =============================================================

def _text(ax, x, y, s, *, size=8, bold=False, color=COL_BLACK,
          ha="left", va="center", rotation=0):
    """日本語対応テキスト描画。"""
    fp = _FP_BOLD if bold else _FP_REGULAR
    return ax.text(
        x, y, s, fontproperties=fp, fontsize=size, color=color,
        ha=ha, va=va, rotation=rotation, zorder=10,
    )


def _line(ax, x0, y0, x1, y1, *, color=COL_BLACK, lw=0.6, ls="-", zorder=5):
    ax.add_line(Line2D([x0, x1], [y0, y1], color=color, lw=lw,
                       linestyle=ls, zorder=zorder, solid_capstyle="butt"))


def _rect(ax, x, y, w, h, *, edge=COL_BLACK, face="none", lw=0.6, zorder=5):
    ax.add_patch(Rectangle((x, y), w, h, fill=(face != "none"),
                           facecolor=face, edgecolor=edge, lw=lw, zorder=zorder))


def _draw_border(ax) -> None:
    """紙全体の外枠（細い黒の図枠）。"""
    _rect(ax, 0.012, 0.012, 1 - 0.024, 1 - 0.024, edge=COL_BLACK, lw=1.0)


def _draw_wordmark(ax) -> None:
    """左上の SANEI ワードマーク（簡素な文字組）。"""
    _text(ax, 0.030, 0.972, COMPANY["slogan"], size=5.5, color=COL_NAVY)
    # ロゴ風: 丸記号 + SANEI
    ax.add_patch(Circle((0.038, 0.945), 0.011, fill=False,
                        edgecolor=COL_NAVY, lw=1.4, zorder=10))
    _text(ax, 0.0335, 0.945, "e", size=8, bold=True, color=COL_NAVY, ha="center")
    _text(ax, 0.052, 0.945, "S A N E I", size=15, bold=True, color=COL_NAVY)
    _text(ax, 0.052, 0.918, "株式会社サンエー", size=7, bold=True, color=COL_NAVY)


def _ellipse_pts(cx, cy, r, aspect, n=48):
    """中心(cx,cy)・半径 r の「画面上で真円に見える」楕円の頂点列を返す。

    set_aspect で y が圧縮されているため、データ y 半径を r/aspect に伸ばす。
    """
    return [
        (cx + r * math.cos(t), cy + (r / aspect) * math.sin(t))
        for t in [2 * math.pi * i / n for i in range(n + 1)]
    ]


def _draw_compass(ax, aspect) -> None:
    """右上の方位記号（N↑ + 簡易コンパスローズ）。

    実サンプル準拠で、小ぶりな二重円のローズに北向きの細い菱形針を載せる。
    set_aspect による y 圧縮を吸収するため、円・y方向の量はすべて /aspect で補正。
    """
    cx, cy = 0.865, 0.855
    r = 0.022
    ry = r / aspect  # データ y 方向半径（真円表示用）

    # ローズ本体（二重円）
    ax.add_line(Line2D(*zip(*_ellipse_pts(cx, cy, r, aspect)),
                       color=COL_BLACK, lw=0.7, zorder=8))
    ax.add_line(Line2D(*zip(*_ellipse_pts(cx, cy, r * 0.6, aspect)),
                       color=COL_BLACK, lw=0.5, zorder=8))

    # 8方向の星形（先端を外周へ）
    for k in range(8):
        ang = math.pi / 2 - k * (math.pi / 4)
        rr = r if (k % 2 == 0) else r * 0.62
        x1 = cx + rr * math.cos(ang)
        y1 = cy + (rr / aspect) * math.sin(ang)
        side = r * 0.14
        ax.add_patch(Polygon([
            (cx + side * math.cos(ang + math.pi / 2),
             cy + (side / aspect) * math.sin(ang + math.pi / 2)),
            (x1, y1),
            (cx + side * math.cos(ang - math.pi / 2),
             cy + (side / aspect) * math.sin(ang - math.pi / 2)),
        ], closed=True, fill=False, edgecolor=COL_BLACK, lw=0.4, zorder=8))

    # 北向きの細い菱形針（ローズ上端から上へ伸ばす）
    tip_y = cy + ry + 0.045 / aspect
    neck_y = cy + ry * 0.4
    half = 0.005
    ax.add_patch(Polygon([
        (cx, tip_y), (cx - half, neck_y), (cx, cy), (cx + half, neck_y),
    ], closed=True, fill=False, edgecolor=COL_BLACK, lw=0.8, zorder=9))
    # N ラベル
    _text(ax, cx, tip_y + 0.018 / aspect, "N", size=11, bold=True, ha="center",
          va="bottom")


# =============================================================
# メイン作図域（屋根プラン）
# =============================================================

def _faces_extent_mm(spec: DraftingSpec) -> tuple:
    """全屋根面の図面上 mm 範囲 (minx,miny,maxx,maxy) を返す。"""
    xs0, ys0, xs1, ys1 = [], [], [], []
    for f in spec.roof_faces:
        ox, oy = f.origin_x_mm, f.origin_y_mm
        if f.shape == "polygon" and f.polygon_mm:
            pxs = [ox + p[0] for p in f.polygon_mm]
            pys = [oy + p[1] for p in f.polygon_mm]
            xs0.append(min(pxs)); ys0.append(min(pys))
            xs1.append(max(pxs)); ys1.append(max(pys))
        else:
            xs0.append(ox); ys0.append(oy)
            xs1.append(ox + f.width_mm); ys1.append(oy + f.depth_mm)
    if not xs0:
        return (0, 0, 10000, 6000)
    return (min(xs0), min(ys0), max(xs1), max(ys1))


def _draw_main_plan(ax, spec: DraftingSpec, box: tuple, aspect: float) -> None:
    """メイン作図域に屋根プラン（外形・ハッチ・パネル・寸法）を描く。

    box = (x0,y0,w,h) は図上正規化座標での描画枠。
    mm→図上は等比スケール（アスペクト保持）で写像する。
    """
    bx0, by0, bw, bh = box
    minx, miny, maxx, maxy = _faces_extent_mm(spec)
    span_x = max(maxx - minx, 1.0)
    span_y = max(maxy - miny, 1.0)

    # 寸法線・ラベルの余白を考慮し、描画枠の内側に収める
    pad = 0.07  # 枠内側余白（正規化）
    inner_w = bw * (1 - 2 * pad)
    inner_h = bh * (1 - 2 * pad)

    # 等比スケール: 図上1単位あたり mm（x方向）。y は aspect 補正。
    # x: 図幅 inner_w に span_x mm、y: 図高 inner_h に span_y mm を収める。
    # ただし円・正方形が歪まないよう、実スケール s [図x単位/mm] を共通に取る。
    s_x = inner_w / span_x
    s_y = (inner_h * aspect) / span_y  # y は aspect で実比へ
    s = min(s_x, s_y)  # 図x単位/mm（実スケール）

    used_w = span_x * s
    used_h = span_y * s / aspect
    # 枠内で中央寄せ
    off_x = bx0 + bw * pad + (inner_w - used_w) / 2.0
    off_y = by0 + bh * pad + (inner_h - used_h) / 2.0

    def mx(x_mm: float) -> float:
        return off_x + (x_mm - minx) * s

    def my(y_mm: float) -> float:
        # 図面 y(下方向+) を上下反転して図上 y(上方向+) に。
        return off_y + used_h - (y_mm - miny) * (s / aspect)

    # 実スケールを記録（縮尺自動算出に使用）。
    # s = 図x単位/mm。図1単位 = 紙幅 297(or420)mm 相当。
    paper_w_mm = 420.0 if (spec.paper or "A4").upper() == "A3" else 297.0
    mm_per_fig = paper_w_mm  # 図全幅(=1.0)が紙幅 mm に対応
    # 実物 mm が紙上 何mm になるか: drawn_mm = s * mm_per_fig ... per real mm
    real_scale = s * mm_per_fig  # 紙上mm / 実物mm
    spec._auto_scale_ratio = real_scale  # type: ignore[attr-defined]

    is_corp = _is_corporate(spec)
    roof_dim_col = COL_GREEN if is_corp else COL_RED

    # --- 各屋根面 ---
    for face in spec.roof_faces:
        _draw_roof_face(ax, spec, face, mx, my, s, aspect, roof_dim_col)

    # --- 全体寸法（屋根全幅/全高）と パネル域寸法 ---
    _draw_plan_dimensions(ax, spec, mx, my, s, aspect, roof_dim_col)


def _draw_roof_face(ax, spec, face: RoofFace, mx, my, s, aspect, roof_dim_col):
    """1 屋根面の外形・ハッチ・パネルを描く。"""
    ox, oy = face.origin_x_mm, face.origin_y_mm

    # 外形頂点（図上）
    if face.shape == "polygon" and face.polygon_mm:
        verts = [(mx(ox + px), my(oy + py)) for px, py in face.polygon_mm]
        ax.add_patch(Polygon(verts, closed=True, fill=False,
                             edgecolor=COL_BLACK, lw=0.8, zorder=4))
        # ハッチ用の外接矩形
        rx0 = mx(ox + min(p[0] for p in face.polygon_mm))
        rx1 = mx(ox + max(p[0] for p in face.polygon_mm))
        ry_top = my(oy + min(p[1] for p in face.polygon_mm))
        ry_bot = my(oy + max(p[1] for p in face.polygon_mm))
        clip_verts = verts
    else:
        rx0 = mx(ox)
        rx1 = mx(ox + face.width_mm)
        ry_top = my(oy)
        ry_bot = my(oy + face.depth_mm)
        _rect(ax, rx0, ry_bot, rx1 - rx0, ry_top - ry_bot,
              edge=COL_BLACK, lw=0.8, zorder=4)
        clip_verts = [(rx0, ry_bot), (rx1, ry_bot), (rx1, ry_top), (rx0, ry_top)]

    # ハッチング（屋根材表現）
    _draw_hatch(ax, face, rx0, rx1, ry_bot, ry_top, clip_verts)

    # パネル
    is_string = spec.drawing_type == DrawingType.STRING
    _draw_panels(ax, spec, face, mx, my, is_string)


def _draw_hatch(ax, face: RoofFace, rx0, rx1, ry_bot, ry_top, clip_verts):
    """屋根内に等間隔の細い灰色ハッチ線。

    瓦/スレート/horizontal → 水平線、折板/vertical → 垂直線、陸屋根/none → なし。
    ポリゴンの場合は外接矩形いっぱいに引く（簡略）。
    """
    hatch = face.hatch
    if not hatch or hatch == "none":
        # roof_type からも判定（保険）
        if face.roof_type in (RoofType.KAWARA, RoofType.SLATE):
            hatch = "horizontal"
        elif face.roof_type == RoofType.SETSUBAN:
            hatch = "vertical"
        else:
            return
    clip_path = Polygon(clip_verts, closed=True, transform=ax.transData)

    if hatch == "horizontal":
        n = 26  # 線本数（瓦の段表現）
        ys = [ry_bot + (ry_top - ry_bot) * (i + 0.5) / n for i in range(n)]
        for y in ys:
            ln = Line2D([rx0, rx1], [y, y], color=COL_HATCH, lw=0.35, zorder=3)
            ln.set_clip_path(clip_path)
            ax.add_line(ln)
    elif hatch == "vertical":
        n = 40  # 折板の山数（細かく）
        xs = [rx0 + (rx1 - rx0) * (i + 0.5) / n for i in range(n)]
        for x in xs:
            ln = Line2D([x, x], [ry_bot, ry_top], color=COL_HATCH, lw=0.35,
                        zorder=3)
            ln.set_clip_path(clip_path)
            ax.add_line(ln)


def _draw_panels(ax, spec, face: RoofFace, mx, my, is_string: bool) -> None:
    """配置済みパネル群を描く。

    配置図: マゼンタ細枠・塗り無し。
    ストリングス図: string_id ごとに色分け + 内部に赤の簡易結線（横折れ線）。
    """
    ox, oy = face.origin_x_mm, face.origin_y_mm

    # ストリング色割当
    sid_order = []
    for pr in face.panels:
        sid = pr.string_id or "1"
        if sid not in sid_order:
            sid_order.append(sid)
    color_of = {
        sid: STRING_COLORS[i % len(STRING_COLORS)]
        for i, sid in enumerate(sid_order)
    }

    # ストリングごとにパネル矩形をまとめておく（結線描画用）
    string_centers: dict = {}

    for pr in face.panels:
        px0 = mx(ox + pr.x_mm)
        px1 = mx(ox + pr.x_mm + pr.w_mm)
        py_top = my(oy + pr.y_mm)
        py_bot = my(oy + pr.y_mm + pr.h_mm)
        x = min(px0, px1)
        y = min(py_top, py_bot)
        w = abs(px1 - px0)
        h = abs(py_top - py_bot)
        if is_string:
            col = color_of.get(pr.string_id or "1", COL_MAGENTA)
            _rect(ax, x, y, w, h, edge=col, lw=0.7, zorder=6)
            cx, cy = x + w / 2, y + h / 2
            string_centers.setdefault(pr.string_id or "1", []).append((cx, cy))
        else:
            _rect(ax, x, y, w, h, edge=COL_MAGENTA, lw=0.8, zorder=6)

    # ストリングス図: 各系統内を赤の折れ線で結ぶ（横方向に蛇行）
    if is_string:
        for sid, pts in string_centers.items():
            if len(pts) < 2:
                continue
            # y(行) でグルーピングし、行内は x 昇順、行間で蛇行
            pts_sorted = sorted(pts, key=lambda p: (round(p[1], 4), p[0]))
            xs = [p[0] for p in pts_sorted]
            ys = [p[1] for p in pts_sorted]
            ax.add_line(Line2D(xs, ys, color=COL_RED, lw=0.6, zorder=7,
                               solid_capstyle="round"))
            # 端部に丸マーカー（結線端子の表現）
            ax.add_patch(Circle((xs[0], ys[0]), 0.0035, fill=False,
                                edgecolor=COL_RED, lw=0.5, zorder=8))


def _draw_plan_dimensions(ax, spec, mx, my, s, aspect, roof_dim_col) -> None:
    """主要寸法線を描く。

    最低限: パネル域 横・縦、左右マージン、屋根全幅。
    法人=屋根寸法を緑系、住宅=赤。パネル寸法は常に赤。
    """
    # 代表面（最初の面）を基準にパネル域とマージンの寸法を出す
    if not spec.roof_faces:
        return
    face = spec.roof_faces[0]
    ox, oy = face.origin_x_mm, face.origin_y_mm

    # 屋根外形（代表面）の図上座標
    if face.shape == "polygon" and face.polygon_mm:
        rx0 = mx(ox + min(p[0] for p in face.polygon_mm))
        rx1 = mx(ox + max(p[0] for p in face.polygon_mm))
        ry_top = my(oy + min(p[1] for p in face.polygon_mm))
        ry_bot = my(oy + max(p[1] for p in face.polygon_mm))
        roof_w_mm = max(p[0] for p in face.polygon_mm) - min(p[0] for p in face.polygon_mm)
        roof_h_mm = max(p[1] for p in face.polygon_mm) - min(p[1] for p in face.polygon_mm)
    else:
        rx0 = mx(ox)
        rx1 = mx(ox + face.width_mm)
        ry_top = my(oy)
        ry_bot = my(oy + face.depth_mm)
        roof_w_mm = face.width_mm
        roof_h_mm = face.depth_mm

    # パネル域の外接（代表面の panels から）
    if face.panels:
        pmin_x = min(p.x_mm for p in face.panels)
        pmax_x = max(p.x_mm + p.w_mm for p in face.panels)
        pmin_y = min(p.y_mm for p in face.panels)
        pmax_y = max(p.y_mm + p.h_mm for p in face.panels)
        gx0 = mx(ox + pmin_x)
        gx1 = mx(ox + pmax_x)
        gy_top = my(oy + pmin_y)
        gy_bot = my(oy + pmax_y)
        panel_w_mm = round(pmax_x - pmin_x)
        panel_h_mm = round(pmax_y - pmin_y)
        left_margin_mm = round(pmin_x)
        right_margin_mm = round(roof_w_mm - pmax_x)
    else:
        return

    off = 0.022  # 寸法線の屋根外形からの逃がし量（正規化）

    # --- 上辺: 左マージン / パネル域横 / 右マージン ---
    dim_y = ry_top + off
    _dim_h(ax, rx0, gx0, dim_y, f"{left_margin_mm}", roof_dim_col, aspect)
    _dim_h(ax, gx0, gx1, dim_y, f"{panel_w_mm}", COL_RED, aspect)
    _dim_h(ax, gx1, rx1, dim_y, f"{right_margin_mm}", roof_dim_col, aspect)

    # --- 左辺: パネル域縦 ---
    dim_x = rx0 - off
    _dim_v(ax, dim_x, gy_top, gy_bot, f"{panel_h_mm}", COL_RED, aspect)

    # --- 右辺: 屋根全高 ---
    dim_x_r = rx1 + off
    _dim_v(ax, dim_x_r, ry_top, ry_bot, f"{round(roof_h_mm)}", roof_dim_col, aspect)

    # --- 下辺: 屋根全幅（数値は寸法線の下に置き、屋根外形と被らせない） ---
    dim_y_b = ry_bot - off * 1.4
    _dim_h(ax, rx0, rx1, dim_y_b, f"{round(roof_w_mm)}", roof_dim_col, aspect,
           label_below=True)


def _dim_h(ax, x0, x1, y, label, color, aspect, *, tick=0.008,
          label_below=False):
    """水平寸法線（両端に斜め羽根 + 数値）。

    label_below=True で数値を寸法線の下側に置く（屋根外形と重なる下辺寸法用）。
    """
    if abs(x1 - x0) < 1e-6:
        return
    _line(ax, x0, y, x1, y, color=color, lw=0.5, zorder=9)
    # 補助線（屋根外形へ向けて短く）
    th = tick / aspect
    for xe in (x0, x1):
        # 斜め羽根（45度）
        _line(ax, xe - tick, y - th, xe + tick, y + th, color=color, lw=0.5,
              zorder=9)
    if label_below:
        _text(ax, (x0 + x1) / 2, y - th * 1.4, label, size=6.5, color=color,
              ha="center", va="top")
    else:
        _text(ax, (x0 + x1) / 2, y + th * 1.4, label, size=6.5, color=color,
              ha="center", va="bottom")


def _dim_v(ax, x, y0, y1, label, color, aspect, *, tick=0.008):
    """垂直寸法線（両端に斜め羽根 + 中央左に縦書き数値）。"""
    if abs(y1 - y0) < 1e-6:
        return
    _line(ax, x, y0, x, y1, color=color, lw=0.5, zorder=9)
    tw = tick
    th = tick / aspect
    for ye in (y0, y1):
        _line(ax, x - tw, ye - th, x + tw, ye + th, color=color, lw=0.5,
              zorder=9)
    _text(ax, x - tw * 1.4, (y0 + y1) / 2, label, size=6.5, color=color,
          ha="right", va="center", rotation=90)


# =============================================================
# 右カラム
# =============================================================

def _is_corporate(spec: DraftingSpec) -> bool:
    """法人案件か（顧客名に「株式会社」を含む等）。"""
    name = spec.customer_name or ""
    return ("株式会社" in name) or ("有限会社" in name) or ("工業" in name)


def _draw_right_column(ax, spec, x0, w, tb_top, aspect) -> None:
    """右カラム（情報ボックス → 系統表 → 架台断面 → パネル詳細）を上から配置。

    タイトルブロック上端より下にはみ出さないよう、各ブロック間の余白を
    内容量に応じて調整する。
    """
    is_string = spec.drawing_type == DrawingType.STRING
    top = 0.76 if is_string else 0.70  # 文字列図は系統表分だけ上から始める
    bottom_limit = tb_top + 0.045      # この y より下へは置かない
    gap = 0.022

    y = top
    # --- 情報ボックス ---
    y = _draw_info_box(ax, spec, x0, w, y, is_string)
    y -= gap

    # --- 系統表（ストリングス図のみ） ---
    if is_string and spec.strings:
        y = _draw_string_table(ax, spec, x0, w, y, aspect)
        y -= gap

    # --- 架台断面図 ---
    y = _draw_mount_section(ax, spec, x0, w, y, aspect)
    y -= gap

    # --- パネル詳細図（下端制限を超えないよう収める） ---
    _draw_panel_detail(ax, spec, x0, w, y, aspect, bottom_limit)


def _draw_info_box(ax, spec, x0, w, y_top, is_string) -> float:
    """情報ボックス（割付図・モジュール・設置容量・PCS）。下端 y を返す。"""
    spec.recompute_totals()
    lines = [
        ("太陽光発電システム　割付図", True, COL_NAVY, 9.5),
        (f"モジュール：{spec.panel.model or '-'}", False, COL_BLACK, 8),
        (f"設置容量：{_fmt_kw(spec.total_kw)}kW({spec.total_panels}枚)",
         False, COL_BLACK, 8),
    ]
    if is_string and spec.pcs_model:
        lines.append((f"PCS：{spec.pcs_model}×{spec.pcs_count}台",
                      False, COL_BLACK, 8))

    lh = 0.034
    y = y_top
    for s, bold, col, sz in lines:
        _text(ax, x0, y, s, size=sz, bold=bold, color=col, ha="left", va="top")
        y -= lh
    return y


def _fmt_kw(v: float) -> str:
    """kW 表示（実サンプル準拠で小数3桁固定。例: 4.650 / 102.010）。"""
    try:
        return f"{float(v):.3f}" if v else "0.000"
    except Exception:
        return str(v)


def _draw_string_table(ax, spec, x0, w, y_top, aspect) -> float:
    """系統表（番号|系統 の2列表）。赤罫線。下端 y を返す。"""
    rows = []
    for sg in spec.strings:
        # config_text に複数行（＋）が含まれる場合がある
        rows.append((sg.pcs_label, sg.display()))

    n = len(rows) + 1  # ヘッダ + 行
    table_w = w * 0.62
    col1 = table_w * 0.38
    row_h = 0.030
    table_h = n * row_h
    tx = x0
    ty = y_top
    # 外枠
    _rect(ax, tx, ty - table_h, table_w, table_h, edge=COL_RED, lw=0.8, zorder=8)
    # 縦罫
    _line(ax, tx + col1, ty, tx + col1, ty - table_h, color=COL_RED, lw=0.6,
          zorder=8)
    # ヘッダ
    _line(ax, tx, ty - row_h, tx + table_w, ty - row_h, color=COL_RED, lw=0.6,
          zorder=8)
    _text(ax, tx + col1 / 2, ty - row_h / 2, "番号", size=7, bold=True,
          color=COL_RED, ha="center")
    _text(ax, tx + col1 + (table_w - col1) / 2, ty - row_h / 2, "系統",
          size=7, bold=True, color=COL_RED, ha="center")
    # 各行
    yy = ty - row_h
    for label, conf in rows:
        _line(ax, tx, yy - row_h, tx + table_w, yy - row_h, color=COL_RED,
              lw=0.5, zorder=8)
        _text(ax, tx + col1 / 2, yy - row_h / 2, label, size=7, color=COL_RED,
              ha="center")
        # 系統は「＋」で改行表現（複数構成）
        conf_disp = conf.replace("＋", "\n").replace("+", "\n")
        _text(ax, tx + col1 + (table_w - col1) / 2, yy - row_h / 2, conf_disp,
              size=6.5, color=COL_RED, ha="center")
        yy -= row_h
    return ty - table_h


def _draw_mount_section(ax, spec, x0, w, y_top, aspect) -> float:
    """架台断面図（傾斜パネル + 架台脚の簡易スケッチ）。下端 y を返す。"""
    mount = spec.mount_type or MountType.YANE
    # 図枠（中央寄せ）
    sec_w = w * 0.7
    sec_x = x0 + (w - sec_w) / 2
    base_y = y_top - 0.085  # 地面ライン
    plat_y = base_y

    if mount == MountType.RIKU:
        # 陸屋根用: ほぼ水平な架台（やや傾斜）+ 短い脚
        x1, x2 = sec_x, sec_x + sec_w
        tilt = 0.012 / aspect
        _line(ax, x1, plat_y + tilt, x2, plat_y, color=COL_BLACK, lw=1.0)
        # ハッチ風の斜線（パネル面）
        for i in range(8):
            xa = x1 + (x2 - x1) * i / 8
            _line(ax, xa, plat_y + tilt * (1 - i / 8), xa + 0.006,
                  plat_y + tilt * (1 - i / 8) - 0.006 / aspect,
                  color=COL_BLACK, lw=0.4)
        # 脚
        for xx in (x1 + sec_w * 0.2, x1 + sec_w * 0.5, x1 + sec_w * 0.8):
            _line(ax, xx, plat_y, xx, plat_y - 0.018 / aspect, color=COL_BLACK,
                  lw=0.6)
        _line(ax, x1, plat_y - 0.018 / aspect, x2, plat_y - 0.018 / aspect,
              color=COL_BLACK, lw=0.6)
    elif mount == MountType.SETSUBAN:
        # 折板屋根用: 折板（ジグザグ）の上に薄いパネル板
        x1, x2 = sec_x, sec_x + sec_w
        # 折板のジグザグ
        zz_x, zz_y = [], []
        n = 8
        for i in range(n + 1):
            zz_x.append(x1 + (x2 - x1) * i / n)
            zz_y.append(plat_y - (0.0 if i % 2 == 0 else 0.010 / aspect))
        ax.add_line(Line2D(zz_x, zz_y, color=COL_BLACK, lw=0.6))
        # パネル板（水平）+ 取付金具
        _line(ax, x1, plat_y + 0.014 / aspect, x2, plat_y + 0.014 / aspect,
              color=COL_BLACK, lw=1.0)
        for xx in (x1 + sec_w * 0.25, x1 + sec_w * 0.55, x1 + sec_w * 0.85):
            ax.add_patch(Polygon([
                (xx - 0.004, plat_y), (xx, plat_y + 0.014 / aspect),
                (xx + 0.004, plat_y)], closed=True, fill=True,
                facecolor=COL_BLACK, edgecolor=COL_BLACK, lw=0.3))
    elif mount == MountType.TEIJUSHIN:
        # 低重心架台: 傾斜の小さい三角架台
        x1, x2 = sec_x, sec_x + sec_w
        h = 0.020 / aspect
        # 傾斜パネル
        _line(ax, x1, plat_y, x2, plat_y + h, color=COL_MAGENTA, lw=1.2)
        # 三角の支え
        _line(ax, x2, plat_y + h, x2, plat_y, color=COL_BLACK, lw=0.7)
        _line(ax, x1, plat_y, x2, plat_y, color=COL_BLACK, lw=0.7)
        _line(ax, x1 + sec_w * 0.5, plat_y, x1 + sec_w * 0.5,
              plat_y + h * 0.5, color=COL_BLACK, lw=0.5)
    else:
        # 屋根用架台（既定）: 平板パネル + 短い金具
        x1, x2 = sec_x, sec_x + sec_w
        _line(ax, x1, plat_y, x2, plat_y, color=COL_BLACK, lw=1.2)
        # パネル面ハッチ
        for i in range(10):
            xa = x1 + (x2 - x1) * i / 10
            _line(ax, xa, plat_y, xa + 0.004, plat_y - 0.004 / aspect,
                  color=COL_BLACK, lw=0.35)
        # 取付金具（小さな脚）
        for xx in (x1 + sec_w * 0.15, x1 + sec_w * 0.4, x1 + sec_w * 0.6,
                   x1 + sec_w * 0.85):
            _line(ax, xx, plat_y, xx, plat_y - 0.012 / aspect, color=COL_BLACK,
                  lw=0.5)
        # 「パネル」引出ラベル
        _text(ax, x1 + sec_w * 0.25, plat_y + 0.05 / aspect, "パネル", size=6.5,
              ha="center")
        _text(ax, x1 + sec_w * 0.7, plat_y + 0.05 / aspect, "パネル", size=6.5,
              ha="center")
        _line(ax, x1 + sec_w * 0.25, plat_y + 0.042 / aspect,
              x1 + sec_w * 0.3, plat_y + 0.002, color=COL_BLACK, lw=0.4)
        _line(ax, x1 + sec_w * 0.7, plat_y + 0.042 / aspect,
              x1 + sec_w * 0.65, plat_y + 0.002, color=COL_BLACK, lw=0.4)

    # ラベル（架台種別文字列）
    _text(ax, x0 + w / 2, base_y - 0.05 / aspect, mount, size=8.5, bold=True,
          ha="center")
    return base_y - 0.07 / aspect


def _draw_panel_detail(ax, spec, x0, w, y_top, aspect,
                       bottom_limit: float = 0.13) -> None:
    """パネル詳細図（1枚拡大 + long/short と gap 寸法）。

    bottom_limit より下にはみ出さないよう、必要なら箱の高さを縮める。
    """
    p = spec.panel
    # 詳細図の枠
    box_w = w * 0.72
    box_x = x0 + (w - box_w) / 2
    box_h = box_w * 0.62 / aspect
    box_y = y_top - box_h
    # 下端制限を超える場合は、上端を保ったまま高さを圧縮
    if box_y < bottom_limit:
        box_y = bottom_limit
        box_h = max(y_top - box_y, 0.04)
    _rect(ax, box_x, box_y, box_w, box_h, edge=COL_BLACK, lw=0.6, zorder=7)

    # 内部: 2×2 のマゼンタ格子（パネル4枚相当）で long/short と gap を見せる
    pad = box_w * 0.18
    gx0 = box_x + pad
    gx1 = box_x + box_w - pad * 0.7
    gy0 = box_y + box_h * 0.22
    gy1 = box_y + box_h * 0.78
    midx = (gx0 + gx1) / 2
    midy = (gy0 + gy1) / 2
    gap_px = box_w * 0.012  # 見た目上の隙間

    # 4枚（2x2）の矩形をマゼンタで
    cells = [
        (gx0, midy + gap_px, midx - gap_px, gy1),         # 左上
        (midx + gap_px, midy + gap_px, gx1, gy1),         # 右上
        (gx0, gy0, midx - gap_px, midy - gap_px),         # 左下
        (midx + gap_px, gy0, gx1, midy - gap_px),         # 右下
    ]
    for cx0, cy0, cx1, cy1 in cells:
        _rect(ax, cx0, cy0, cx1 - cx0, cy1 - cy0, edge=COL_MAGENTA, lw=0.9,
              zorder=8)

    # 上辺寸法: long_mm（横向き時は長辺が横）。サンプル準拠で long×2 表示。
    long_lbl = f"{int(p.long_mm)}" if p.long_mm else "-"
    short_lbl = f"{int(p.short_mm)}" if p.short_mm else "-"
    _dim_h(ax, gx0, midx - gap_px, gy1 + 0.014 / aspect, long_lbl, COL_BLACK,
           aspect, tick=0.005)
    _dim_h(ax, midx + gap_px, gx1, gy1 + 0.014 / aspect, long_lbl, COL_BLACK,
           aspect, tick=0.005)
    # 左辺寸法: short_mm（上下2枚分）
    _dim_v(ax, gx0 - 0.012, midy + gap_px, gy1, short_lbl, COL_BLACK, aspect,
           tick=0.005)
    _dim_v(ax, gx0 - 0.012, gy0, midy - gap_px, short_lbl, COL_BLACK, aspect,
           tick=0.005)
    # gap 寸法（縦25 / 横10 等）: 中央の隙間に数値
    _text(ax, midx - gap_px * 3, midy, f"{int(p.gap_long_mm)}", size=5.5,
          color=COL_BLACK, ha="right", va="center", rotation=90)
    _text(ax, midx, midy + gap_px * 3, f"{int(p.gap_short_mm)}", size=5.5,
          color=COL_BLACK, ha="center", va="bottom")

    # ラベル
    _text(ax, box_x + box_w / 2, box_y - 0.022, "パネル", size=8.5, bold=True,
          ha="center", va="top")


# =============================================================
# タイトルブロック
# =============================================================

def _resolve_scale(spec: DraftingSpec) -> str:
    """縮尺文字列を決める（title.scale 優先、空なら実スケールから自動）。"""
    if spec.title and spec.title.scale:
        return spec.title.scale
    ratio = getattr(spec, "_auto_scale_ratio", None)
    paper = "A3" if (spec.paper or "A4").upper() == "A3" else "A4"
    if not ratio or ratio <= 0:
        return f"1/100({paper})"
    denom = 1.0 / ratio  # 実物mm / 紙上mm
    best = min(_SCALE_DENOMS, key=lambda d: abs(d - denom))
    return f"1/{best}({paper})"


def _draw_title_block(ax, spec, tb_top, aspect) -> None:
    """下部タイトルブロック（3行 + 右端の会社ブロック）。"""
    t = spec.title
    x0 = 0.012
    x1 = 1 - 0.012
    y0 = 0.012
    y1 = tb_top
    full_w = x1 - x0
    full_h = y1 - y0

    # 会社ブロックの幅（右端）
    comp_w = full_w * 0.235
    grid_x1 = x1 - comp_w  # 表部の右端

    # 外枠
    _rect(ax, x0, y0, full_w, full_h, edge=COL_BLACK, lw=1.0, zorder=8)
    # 会社ブロック仕切り
    _line(ax, grid_x1, y0, grid_x1, y1, color=COL_BLACK, lw=1.0, zorder=8)

    # --- 表部（左側）: 3行 ---
    row_h = full_h / 3.0
    for i in (1, 2):
        yy = y0 + row_h * i
        _line(ax, x0, yy, grid_x1, yy, color=COL_BLACK, lw=0.6, zorder=8)

    grid_w = grid_x1 - x0
    # 列割: [No./図番] [ラベル] [値（広め）] [ラベル2] [値2]
    c = [
        x0,
        x0 + grid_w * 0.135,
        x0 + grid_w * 0.255,
        x0 + grid_w * 0.72,
        x0 + grid_w * 0.845,
        grid_x1,
    ]
    for cx in c[1:-1]:
        _line(ax, cx, y0, cx, y1, color=COL_BLACK, lw=0.6, zorder=8)

    # 法人/住宅でラベル文言を変える（実サンプル準拠）
    is_corp = _is_corporate(spec)
    label_proj = "工事名称" if is_corp else "指示書番号"
    label_dwg = "図 面 名" if is_corp else "図 面 名 称"

    def cell(col_l, col_r, row_from_top, s, *, bold=False, size=8,
             ha="center", color=COL_BLACK):
        # row_from_top: 0=最上行, 1=中, 2=下
        cy = y1 - row_h * (row_from_top + 0.5)
        cxl = c[col_l]
        cxr = c[col_r]
        if ha == "center":
            cx = (cxl + cxr) / 2
        elif ha == "left":
            cx = cxl + 0.006
        else:
            cx = cxr - 0.006
        _text(ax, cx, cy, s, size=size, bold=bold, color=color, ha=ha,
              va="center")

    # 行1（図番。既に "No." で始まる場合は二重付与しない）
    _dno = (t.drawing_no or "").strip()
    if _dno and not _dno.lower().lstrip().startswith("no."):
        _dno = f"No.{_dno}"
    cell(0, 1, 0, _dno, size=7.5, ha="center")
    cell(1, 2, 0, label_proj, size=8)
    cell(2, 3, 0, t.project_name, size=8, ha="center")
    cell(3, 4, 0, "設置角度", size=8)
    cell(4, 5, 0, t.install_angle, size=8)
    # 行2
    cell(1, 2, 1, label_dwg, size=8)
    cell(2, 3, 1, t.drawing_name, size=9, ha="center")
    cell(3, 4, 1, "縮 尺", size=8)
    cell(4, 5, 1, _resolve_scale(spec), size=8)
    # 行3
    cell(1, 2, 2, "システム", size=8)
    cell(2, 3, 2, t.system_text, size=8, ha="center")
    cell(3, 4, 2, "作 成 日", size=8)
    cell(4, 5, 2, t.created_date, size=8)

    # --- 会社ブロック（右端）---
    ccx = grid_x1 + comp_w / 2
    _text(ax, ccx, y1 - full_h * 0.34, COMPANY["name"], size=15, bold=True,
          color=COL_BLACK, ha="center")
    _text(ax, ccx, y1 - full_h * 0.62,
          f'{COMPANY["postal"]}　{COMPANY["address"]}', size=6.5,
          color=COL_BLACK, ha="center")
    _text(ax, ccx, y1 - full_h * 0.82,
          f'{COMPANY["tel"]}　{COMPANY["fax"]}', size=6.5,
          color=COL_BLACK, ha="center")


# =============================================================
# 自己テスト
# =============================================================

def _selftest_fill_grid(spec: DraftingSpec) -> None:
    """自己テスト用: layout_engine に依存せず panels を埋める。"""
    for face in spec.roof_faces:
        face.panels = []  # 確実に自前グリッドで埋める
        _fill_simple_grid(spec, face)
    spec.recompute_totals()


def _main() -> None:
    import os
    from drafting.sample_specs import GOLDEN_SPECS, get_golden

    out_dir = "/tmp/seizu_render"
    os.makedirs(out_dir, exist_ok=True)

    print("=== drawing_renderer 自己テスト ===")
    results = []
    for name in GOLDEN_SPECS:
        spec = get_golden(name)
        _selftest_fill_grid(spec)
        try:
            out = render_drawing(spec, dpi=150)
        except Exception as e:  # noqa: BLE001
            print(f"[NG] {name}: render 失敗: {e!r}")
            raise
        png_path = os.path.join(out_dir, f"{name}.png")
        pdf_path = os.path.join(out_dir, f"{name}.pdf")
        with open(png_path, "wb") as f:
            f.write(out["png_bytes"])
        with open(pdf_path, "wb") as f:
            f.write(out["pdf_bytes"])
        png_sz = os.path.getsize(png_path)
        pdf_sz = os.path.getsize(pdf_path)
        ok = png_sz > 0 and pdf_sz > 0
        results.append((name, png_path, png_sz, pdf_path, pdf_sz, ok))
        status = "OK" if ok else "NG"
        print(f"[{status}] {name}: panels={spec.total_panels} "
              f"png={png_sz}B pdf={pdf_sz}B")
        print(f"       {png_path}")
        print(f"       {pdf_path}")

    all_ok = all(r[5] for r in results)
    print(f"\n結果: {'全て成功' if all_ok else '失敗あり'} "
          f"({sum(1 for r in results if r[5])}/{len(results)})")


if __name__ == "__main__":
    _main()
