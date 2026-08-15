"""簡易製図AI データモデル（全エンジン共通の契約）

spec_extractor / layout_engine / drawing_renderer が共有するデータ構造。
寸法はすべて mm（ミリメートル）を基準単位とする（CAD図面慣習）。

設計方針:
- DraftingSpec が単一の真実。extractor が屋根面・モジュール・枚数・系統を埋め、
  layout_engine が各屋根面の panels（PanelRect 群）を埋め、renderer が描画する。
- JSON ラウンドトリップ可能（spec_to_dict / spec_from_dict）。
  Vision抽出 → 確認フォーム編集 → セッション保存 → 再描画 を JSON で往復する。
- Enum は値が日本語/英字の素の str。フォームやJSONでそのまま扱える。
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Optional


# =============================================================
# 区分（素の str で扱える定数群）
# =============================================================

class DrawingType:
    """図面種別。"""
    LAYOUT = "layout"   # 太陽光配置図 / レイアウト図 / 割付図
    STRING = "string"   # ストリングス図（PCS系統で色分け＋系統表）
    EQUIPMENT = "equipment"  # 機器配置図（将来拡張・今回はスコープ外フック）

    ALL = (LAYOUT, STRING, EQUIPMENT)
    LABEL = {
        LAYOUT: "太陽光配置図",
        STRING: "ストリングス図",
        EQUIPMENT: "機器配置図",
    }


class RoofType:
    """屋根種別（ハッチング表現と既定架台に影響）。"""
    KAWARA = "kawara"        # 瓦屋根（横ハッチング）
    SETSUBAN = "setsuban"    # 折板屋根（縦ハッチング・縦置きパネル）
    RIKUYANE = "rikuyane"    # 陸屋根（ハッチングなし）
    SLATE = "slate"          # スレート屋根（横ハッチング）

    ALL = (KAWARA, SETSUBAN, RIKUYANE, SLATE)
    LABEL = {
        KAWARA: "瓦屋根",
        SETSUBAN: "折板屋根",
        RIKUYANE: "陸屋根",
        SLATE: "スレート屋根",
    }


class Orientation:
    """パネルの向き。"""
    PORTRAIT = "portrait"     # 縦置き（長辺が屋根の奥行き方向）
    LANDSCAPE = "landscape"   # 横置き（長辺が屋根の幅方向）
    AUTO = "auto"             # 枚数最大化で自動選択

    ALL = (PORTRAIT, LANDSCAPE, AUTO)
    LABEL = {
        PORTRAIT: "縦置き",
        LANDSCAPE: "横置き",
        AUTO: "自動",
    }


class MountType:
    """架台種別（架台断面図の出し分け）。"""
    YANE = "屋根用架台"
    TEIJUSHIN = "低重心架台"
    RIKU = "陸屋根用"
    SETSUBAN = "折板屋根用"

    ALL = (YANE, TEIJUSHIN, RIKU, SETSUBAN)


# =============================================================
# パネル
# =============================================================

@dataclass
class PanelSpec:
    """太陽光モジュール（パネル）1枚の仕様。"""
    maker: str = ""                 # 例: NEXT ENERGY / XSOL / SHARP / JINKO
    model: str = ""                 # 例: NER108M465B-NE
    output_w: float = 0.0           # 1枚あたり出力 W（例: 465）
    long_mm: float = 0.0            # パネル長辺 mm（例: 1762 / 1903 / 2278）
    short_mm: float = 0.0           # パネル短辺 mm（例: 1134 / 1170）
    gap_long_mm: float = 25.0       # 長辺側隙間 mm（行間・縦・段と段の間。サンプル標準=25）
    gap_short_mm: float = 10.0      # 短辺側隙間 mm（列間・横・列と列の間。サンプル標準=10）
    # --- 点検通路（2026-07-23 会議 修正①）---
    # N列ごとに人が通れる点検通路を確保する。0 = 通路なし（従来配置）。
    # dataclass 既定は必ず 0.0 のまま（保存済みJSON・学習データの後方互換のため）。
    # 新規下書きのUI既定 800mm は app_pages 側で注入する。
    walkway_mm: float = 0.0          # 点検通路の幅 mm（0=通路なし。UI既定800）
    walkway_every_n_cols: int = 2    # 何列ごとに点検通路を入れるか（既定: 2列ごと）

    def area_sqm(self) -> float:
        return (self.long_mm / 1000.0) * (self.short_mm / 1000.0)


# =============================================================
# 配置済みパネル（屋根面ローカル座標）
# =============================================================

@dataclass
class PanelRect:
    """屋根面に配置された 1 枚のパネル（屋根面ローカル座標, mm）。

    x_mm, y_mm は屋根面ローカル原点（左上）からのオフセット。
    w_mm, h_mm は図面上の描画寸法（向きを反映済み）。
    """
    x_mm: float
    y_mm: float
    w_mm: float
    h_mm: float
    orientation: str = Orientation.PORTRAIT
    string_id: Optional[str] = None   # 所属ストリング識別子（例 "1-1"）。string図で色分け


# =============================================================
# 屋根面
# =============================================================

@dataclass
class RoofFace:
    """1 つの屋根面。矩形またはポリゴン。

    座標系: 屋根面ローカル mm。原点は左上、x=右(東), y=下(南)。
    複数面を 1 枚の図面に並べるとき origin_x_mm / origin_y_mm で図面上に配置する。
    """
    name: str = "面1"
    roof_type: str = RoofType.KAWARA
    # --- 形状 ---
    shape: str = "rectangle"                 # "rectangle" | "polygon"
    width_mm: float = 0.0                     # 矩形: 幅（東西）
    depth_mm: float = 0.0                     # 矩形: 奥行（南北）
    polygon_mm: Optional[list] = None         # ポリゴン: [[x,y], ...] 閉じない頂点列
    # --- 図面上の配置（複数面用） ---
    origin_x_mm: float = 0.0
    origin_y_mm: float = 0.0
    # --- 配置パラメータ ---
    # 屋根エッジからの離隔。0 = 未指定（作図ルール3条により「各方向寸法の
    # 10%・上限2m」が layout_engine 側で自動適用される）。値を入れた場合は
    # 500 を含むあらゆる値がそのまま尊重される（2026-08-13 顧客提供
    # 「太陽光配置図 作図ルール」準拠。旧既定値は500だった）
    margin_mm: float = 0.0                    # 両方向共通の明示値（0=未指定）
    margin_ns_mm: float = 0.0                 # 南北方向（上下端）の離隔（0=未指定）
    margin_ew_mm: float = 0.0                 # 東西方向（左右端）の離隔（0=未指定）
    orientation: str = Orientation.AUTO       # この面でのパネル向き
    target_panel_count: Optional[int] = None  # この面に置きたい枚数（抽出値・上限）
    # ハッチ向き。空（既定）なら描画時に roof_type から自動判定
    # （瓦/スレート→横, 折板→縦, 陸屋根→無し）。明示時は "horizontal"|"vertical"|"none"。
    hatch: str = ""
    # --- 配置結果（layout_engine が埋める） ---
    panels: list = field(default_factory=list)  # list[PanelRect]
    rows: int = 0
    cols: int = 0
    panel_count: int = 0

    def bounds_mm(self) -> tuple:
        """この面の外接矩形 (w, h) を返す（ポリゴンにも対応）。"""
        if self.shape == "polygon" and self.polygon_mm:
            xs = [p[0] for p in self.polygon_mm]
            ys = [p[1] for p in self.polygon_mm]
            return (max(xs) - min(xs), max(ys) - min(ys))
        return (self.width_mm, self.depth_mm)


# =============================================================
# ストリング系統
# =============================================================

@dataclass
class StringGroup:
    """PCS 系統 1 単位。系統表に出す（例: PCS1 = 12直×5並）。"""
    pcs_label: str = "PCS1"     # 番号ラベル
    series: int = 0             # 直列数（直）
    parallel: int = 0           # 並列数（並）
    config_text: str = ""       # 表示用文字列（"12直×5並" 等）。空なら series/parallel から生成

    def display(self) -> str:
        if self.config_text:
            return self.config_text
        if self.series and self.parallel:
            return f"{self.series}直×{self.parallel}並"
        return ""


# =============================================================
# 機器（機器配置図用・将来拡張フック）
# =============================================================

@dataclass
class Equipment:
    """機器配置図に描く機器（PCS / 盤 / QB室 / 遠 等）。今回はスコープ外。"""
    kind: str = "PCS"           # "PCS" | "盤" | "QB室" | "遠" | "接続箱"
    label: str = ""
    x_mm: float = 0.0
    y_mm: float = 0.0
    w_mm: float = 0.0
    h_mm: float = 0.0


# =============================================================
# タイトルブロック
# =============================================================

@dataclass
class TitleBlock:
    """図面下部のタイトルブロック情報。"""
    drawing_no: str = ""        # 図番（例 No.202605-88TH）
    instruction_no: str = ""    # 指示書番号
    project_name: str = ""      # 工事名称（例 栗原 英信様 太陽光システム増設工事）
    drawing_name: str = "太陽光配置図"  # 図面名
    system_text: str = ""       # システム（例 NEXT ENERGY 465W×10枚 4.650kW）
    install_angle: str = ""     # 設置角度
    scale: str = ""             # 縮尺（例 1/60(A4)）。空なら renderer が自動算出
    created_date: str = ""      # 作成日（例 2026年06月05日）


# =============================================================
# DraftingSpec（ルート）
# =============================================================

@dataclass
class DraftingSpec:
    """製図 1 枚分の完全な仕様。"""
    customer_name: str = ""
    drawing_type: str = DrawingType.LAYOUT
    paper: str = "A4"                       # "A4" | "A3"（いずれも横）

    panel: PanelSpec = field(default_factory=PanelSpec)
    mount_type: str = MountType.YANE

    pcs_model: str = ""                     # 例 CEPT-P3AB2025B
    pcs_count: int = 0

    roof_faces: list = field(default_factory=list)   # list[RoofFace]
    strings: list = field(default_factory=list)      # list[StringGroup]
    equipment: list = field(default_factory=list)    # list[Equipment]

    title: TitleBlock = field(default_factory=TitleBlock)

    # --- 集計（layout 後に再計算される派生値。抽出時の宣言値としても使う） ---
    total_panels: int = 0
    total_kw: float = 0.0

    notes: str = ""
    # フォーム表示用の信頼度メモ（フィールド名→"high"/"medium"/"low" 等）
    confidence: dict = field(default_factory=dict)
    # 抽出時の所見・要確認事項（人間が確認すべき点）
    warnings: list = field(default_factory=list)
    # 設計確定情報の素材（2026-08-15 顧客ルールブック【図面側】10条）。
    # 電圧区分・事業区分・PCS設置位置・配管ルート・配線概算距離・貫通箇所・
    # 盤改造・支給品などを抽出時に取得し、design_handoff.build_design_handoff で
    # 見積側へ引き継ぐ完全な「設計確定情報」に組み立てる。不明は「未確認」。
    handoff: dict = field(default_factory=dict)

    # ---- 派生計算 ----
    def recompute_totals(self) -> "DraftingSpec":
        """全屋根面のパネル数から total_panels / total_kw を再計算する。"""
        faces = [f for f in self.roof_faces if f is not None]
        n = sum(int(f.panel_count or 0) for f in faces)
        if n <= 0:
            # まだ配置前: target_panel_count の合計で代替
            n = sum(int(f.target_panel_count or 0) for f in faces)
        self.total_panels = n
        self.total_kw = round(n * (self.panel.output_w or 0) / 1000.0, 3)
        return self


# =============================================================
# JSON ラウンドトリップ
# =============================================================

def spec_to_dict(spec: DraftingSpec) -> dict:
    """DraftingSpec を JSON 化可能な dict に変換。"""
    return asdict(spec)


def _build_panel(d: dict) -> PanelSpec:
    return PanelSpec(**{k: d.get(k, getattr(PanelSpec(), k)) for k in PanelSpec().__dict__})


def _build_roof_face(d: dict) -> RoofFace:
    base = RoofFace()
    kwargs = {}
    for k in base.__dict__:
        if k == "panels":
            kwargs[k] = [PanelRect(**pr) for pr in (d.get("panels") or [])]
        else:
            kwargs[k] = d.get(k, getattr(base, k))
    return RoofFace(**kwargs)


def spec_from_dict(d: dict) -> DraftingSpec:
    """dict（JSON由来）から DraftingSpec を復元。未知キーは無視、欠損は既定値。"""
    if not isinstance(d, dict):
        return default_spec()
    spec = DraftingSpec()
    spec.customer_name = d.get("customer_name", "")
    spec.drawing_type = d.get("drawing_type", DrawingType.LAYOUT)
    spec.paper = d.get("paper", "A4")
    spec.mount_type = d.get("mount_type", MountType.YANE)
    spec.pcs_model = d.get("pcs_model", "")
    spec.pcs_count = int(d.get("pcs_count", 0) or 0)
    spec.notes = d.get("notes", "")
    spec.confidence = d.get("confidence", {}) or {}
    spec.warnings = d.get("warnings", []) or []
    spec.handoff = d.get("handoff", {}) or {}
    spec.total_panels = int(d.get("total_panels", 0) or 0)
    spec.total_kw = float(d.get("total_kw", 0) or 0)

    spec.panel = _build_panel(d.get("panel", {}) or {})
    spec.roof_faces = [_build_roof_face(f) for f in (d.get("roof_faces") or [])]
    spec.strings = [
        StringGroup(**{k: s.get(k, getattr(StringGroup(), k)) for k in StringGroup().__dict__})
        for s in (d.get("strings") or [])
    ]
    spec.equipment = [
        Equipment(**{k: e.get(k, getattr(Equipment(), k)) for k in Equipment().__dict__})
        for e in (d.get("equipment") or [])
    ]
    t = d.get("title", {}) or {}
    spec.title = TitleBlock(**{k: t.get(k, getattr(TitleBlock(), k)) for k in TitleBlock().__dict__})
    return spec


def default_spec() -> DraftingSpec:
    """空の最小スペック（フォーム初期値用）。"""
    return DraftingSpec(
        customer_name="",
        drawing_type=DrawingType.LAYOUT,
        panel=PanelSpec(),
        roof_faces=[RoofFace(name="面1", width_mm=10000.0, depth_mm=6000.0)],
        title=TitleBlock(),
    )
