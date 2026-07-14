"""屋根種別ごとの配置既定値の読み込み（knowledge/layout_defaults.yaml）

傾斜屋根・陸屋根などの屋根種別に応じた離隔（行間・列間・マージン）の
既定値を返す。YAMLを編集すれば反映され、コード変更は不要。
読み込み失敗時は空 dict を返し、配置計算は標準既定値のまま続行する。
"""
import logging

import yaml

from config import KNOWLEDGE_DIR

logger = logging.getLogger(__name__)

DEFAULTS_PATH = KNOWLEDGE_DIR / "layout_defaults.yaml"


def roof_type_defaults(roof_type: str) -> dict:
    """屋根種別に対応する配置既定値を返す。

    Returns:
        {"gap_long_mm": .., "gap_short_mm": .., "margin_mm": ..} の部分集合。
        定義が無い・読めない場合は {}。
    """
    if not roof_type:
        return {}
    try:
        with open(DEFAULTS_PATH, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        defaults = data.get("roof_type_defaults") or {}
        entry = defaults.get(roof_type)
        return entry if isinstance(entry, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as e:
        logger.warning("layout_defaults.yaml の読み込みに失敗: %s", e)
        return {}
