"""学習ルールのJSONストア（契約）

knowledge/ 配下に学習済みルールと学習ログを保持する。
書き込みは tmp ファイル + os.replace のアトミック方式
（product/product_registry.py と同じパターン）。

Supabase（learning/storage_backend.py）が構成されている場合は
Supabase を正として読み書きし、ローカルファイルは並行保存する
（Streamlit Cloud はコンテナ再起動で実行時ファイルが消えるため）。
"""
import json
import logging
import os
from datetime import datetime
from pathlib import Path

from config import KNOWLEDGE_DIR

logger = logging.getLogger(__name__)

ESTIMATE_RULES_PATH = KNOWLEDGE_DIR / "learned_estimate_rules.json"
DRAWING_RULES_PATH = KNOWLEDGE_DIR / "learned_drawing_rules.json"
LEARNING_LOG_PATH = KNOWLEDGE_DIR / "learning_history.json"

_VALID_TARGETS = ("estimate", "drawing")


def _rules_path(target: str) -> Path:
    if target not in _VALID_TARGETS:
        raise ValueError(f"未知のtarget: {target}")
    return ESTIMATE_RULES_PATH if target == "estimate" else DRAWING_RULES_PATH


def _load_json_list(path: Path, key: str) -> list[dict]:
    """path のJSONから key のリストを読む。無い/壊れている場合は []。"""
    if not path.exists():
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        items = data.get(key, [])
        return items if isinstance(items, list) else []
    except Exception as e:
        logger.warning("学習ストアの読み込みに失敗（%s）: %s", path.name, e)
        return []


def _atomic_write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".json.tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, path)


def _load_doc_list(path: Path, key: str) -> list[dict]:
    """Supabase（構成時）→ ローカルファイルの順でリストを読む。

    KVキーはファイル名の stem（learned_estimate_rules 等）。
    """
    try:
        from learning.storage_backend import is_enabled, kv_get
        if is_enabled():
            doc = kv_get(path.stem)
            if doc is not None:
                items = doc.get(key, [])
                if isinstance(items, list):
                    return items
    except Exception as e:
        logger.warning("Supabase読込に失敗、ローカルにフォールバック: %s", e)
    return _load_json_list(path, key)


def _save_doc(path: Path, data: dict) -> None:
    """ローカルに常に保存し、Supabase 構成時は同内容を upsert する。"""
    _atomic_write(path, data)
    try:
        from learning.storage_backend import is_enabled, kv_set
        if is_enabled() and not kv_set(path.stem, data):
            logger.warning("Supabase保存に失敗（ローカルには保存済み）: %s", path.stem)
    except Exception as e:
        logger.warning("Supabase保存に失敗（ローカルには保存済み）: %s", e)


def load_rules(target: str) -> list[dict]:
    """学習ルール全件（enabled/disabled 含む）を返す。"""
    return _load_doc_list(_rules_path(target), "rules")


def enabled_rules(target: str) -> list[dict]:
    """有効な学習ルールのみを返す。"""
    return [r for r in load_rules(target) if r.get("enabled", True)]


def save_rules(target: str, rules: list[dict]) -> None:
    _save_doc(_rules_path(target), {
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "rules": rules,
    })


def _dedup_key(rule: dict) -> tuple:
    """同一ルールの再学習を上書き更新するための一意キー。

    estimate は match_remarks（正規化備考）もキーに含める。備考違いの同名項目
    （材料費「PVケーブル間」×5等）のルールが互いに上書きされるのを防ぐ。
    旧形式ルール（match_remarks 無し）は "" として後方互換。
    """
    kind = rule.get("kind", "")
    payload = rule.get("payload", {}) or {}
    if kind == "golden_example":
        return (rule.get("target", ""), kind, payload.get("name", ""))
    if rule.get("target") == "drawing":
        return (rule.get("target", ""), kind, payload.get("roof_type", ""))
    return (
        rule.get("target", ""), kind,
        rule.get("category", ""), rule.get("match_description", ""),
        rule.get("match_remarks", "") or "",
    )


def new_rule_id(target: str) -> str:
    prefix = "er" if target == "estimate" else "dr"
    return f"{prefix}-{datetime.now().strftime('%Y%m%d%H%M%S%f')}"


def add_rules(target: str, new_rules: list[dict]) -> list[dict]:
    """ルールを追加保存する。同一キーの既存ルールは上書き更新。全件を返す。"""
    rules = load_rules(target)
    existing_by_key = {_dedup_key(r): i for i, r in enumerate(rules)}
    for idx, rule in enumerate(new_rules):
        rule = dict(rule)
        rule.setdefault("target", target)
        rule.setdefault("enabled", True)
        rule.setdefault("applied_count", 0)
        key = _dedup_key(rule)
        if key in existing_by_key:
            old = rules[existing_by_key[key]]
            rule["id"] = old.get("id") or f"{new_rule_id(target)}-{idx}"
            rule["applied_count"] = old.get("applied_count", 0)
            rules[existing_by_key[key]] = rule
        else:
            rule.setdefault("id", f"{new_rule_id(target)}-{idx}")
            existing_by_key[key] = len(rules)
            rules.append(rule)
    save_rules(target, rules)
    return rules


def set_rule_enabled(target: str, rule_id: str, enabled: bool) -> None:
    rules = load_rules(target)
    for r in rules:
        if r.get("id") == rule_id:
            r["enabled"] = enabled
            break
    save_rules(target, rules)


def delete_rule(target: str, rule_id: str) -> None:
    rules = [r for r in load_rules(target) if r.get("id") != rule_id]
    save_rules(target, rules)


def append_learning_log(entry: dict) -> None:
    """学習セッションの記録を追記する（いつ・何を・何件学習したか）。"""
    logs = _load_doc_list(LEARNING_LOG_PATH, "logs")
    entry = dict(entry)
    entry.setdefault("logged_at", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    logs.append(entry)
    _save_doc(LEARNING_LOG_PATH, {"logs": logs})


def load_learning_log() -> list[dict]:
    return _load_doc_list(LEARNING_LOG_PATH, "logs")
