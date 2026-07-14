"""見積・図面の生成履歴の保存/一覧/読込（契約）

学習の材料として、PDF生成成功時の EstimateData と
製図生成成功時の spec dict を data/ 配下にJSON保存する。
保存失敗は本体フローを止めない（例外を飲んで None を返す）。
"""
import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Optional, Union

from config import BASE_DIR
from models.estimate_data import EstimateData
from learning.models import ParsedEstimate, ParsedLineItem

logger = logging.getLogger(__name__)

ESTIMATE_HISTORY_DIR = BASE_DIR / "data" / "estimate_history"
DRAWING_HISTORY_DIR = BASE_DIR / "data" / "drawing_history"

_SAFE_NAME_RE = re.compile(r"[^0-9A-Za-z぀-ヿ一-鿿_-]+")


def _safe_name(s: str, fallback: str = "noname") -> str:
    s = _SAFE_NAME_RE.sub("_", (s or "").strip())
    return s[:40] or fallback


def _atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".json.tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, path)


# =============================================================
# 見積履歴
# =============================================================

def save_estimate_history(estimate: EstimateData) -> Optional[Path]:
    """見積をJSON保存する。失敗時は None（本体フローを止めない）。"""
    try:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        eid = _safe_name(estimate.cover.estimate_id, "id")
        path = ESTIMATE_HISTORY_DIR / f"{ts}_{eid}.json"
        _atomic_write_json(path, {
            "saved_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "estimate": estimate.model_dump(mode="json"),
        })
        return path
    except Exception as e:
        logger.warning("見積履歴の保存に失敗: %s", e)
        return None


def list_estimate_history() -> list[dict]:
    """見積履歴の一覧（新しい順）。"""
    results = []
    try:
        if not ESTIMATE_HISTORY_DIR.exists():
            return []
        for path in sorted(ESTIMATE_HISTORY_DIR.glob("*.json"), reverse=True):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                est = data.get("estimate", {})
                cover = est.get("cover", {})
                summary = est.get("summary", {})
                results.append({
                    "path": str(path),
                    "estimate_id": cover.get("estimate_id", ""),
                    "client_name": cover.get("client_name", ""),
                    "project_name": cover.get("project_name", ""),
                    "saved_at": data.get("saved_at", ""),
                    "total_with_tax": summary.get("total_with_tax", 0),
                })
            except Exception:
                continue
    except Exception as e:
        logger.warning("見積履歴の一覧取得に失敗: %s", e)
    return results


def load_estimate_history(path: Union[str, Path]) -> Optional[EstimateData]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return EstimateData.model_validate(data.get("estimate", {}))
    except Exception as e:
        logger.warning("見積履歴の読込に失敗（%s）: %s", path, e)
        return None


def estimate_to_parsed(estimate: EstimateData, file_name: str = "") -> ParsedEstimate:
    """EstimateData → ParsedEstimate のロスレス変換（AI見積側の入力）。"""
    items = []
    for cat in estimate.summary.categories:
        for item in cat.items:
            items.append(ParsedLineItem(
                category=cat.category.value,
                no=item.no,
                description=item.description,
                remarks=item.remarks,
                quantity_value=item.quantity_value,
                quantity_unit=item.quantity_unit,
                unit_price=item.unit_price,
                amount=item.amount,
            ))
    return ParsedEstimate(
        source="ai",
        origin="history",
        file_name=file_name,
        estimate_id=estimate.cover.estimate_id,
        client_name=estimate.cover.client_name,
        project_name=estimate.cover.project_name,
        issue_date=estimate.cover.issue_date,
        items=items,
        subtotal=estimate.summary.subtotal,
        discount=estimate.summary.discount,
        total_before_tax=estimate.summary.total_before_tax,
        tax=estimate.summary.tax,
        total_with_tax=estimate.summary.total_with_tax,
    )


# =============================================================
# 図面履歴
# =============================================================

def save_drawing_history(spec_dict: dict) -> Optional[Path]:
    """図面スペックをJSON保存する。失敗時は None。"""
    try:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        cust = _safe_name(spec_dict.get("customer_name", ""), "customer")
        path = DRAWING_HISTORY_DIR / f"{ts}_{cust}.json"
        _atomic_write_json(path, {
            "saved_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "spec": spec_dict,
        })
        return path
    except Exception as e:
        logger.warning("図面履歴の保存に失敗: %s", e)
        return None


def list_drawing_history() -> list[dict]:
    """図面履歴の一覧（新しい順）。"""
    results = []
    try:
        if not DRAWING_HISTORY_DIR.exists():
            return []
        for path in sorted(DRAWING_HISTORY_DIR.glob("*.json"), reverse=True):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                spec = data.get("spec", {})
                results.append({
                    "path": str(path),
                    "customer_name": spec.get("customer_name", ""),
                    "drawing_type": spec.get("drawing_type", ""),
                    "total_panels": spec.get("total_panels", 0),
                    "total_kw": spec.get("total_kw", 0),
                    "saved_at": data.get("saved_at", ""),
                })
            except Exception:
                continue
    except Exception as e:
        logger.warning("図面履歴の一覧取得に失敗: %s", e)
    return results


def load_drawing_history(path: Union[str, Path]) -> Optional[dict]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        spec = data.get("spec")
        return spec if isinstance(spec, dict) else None
    except Exception as e:
        logger.warning("図面履歴の読込に失敗（%s）: %s", path, e)
        return None
