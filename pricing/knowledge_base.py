"""YAMLルール読み込み"""
import yaml
from pathlib import Path
from config import KNOWLEDGE_DIR


def load_pricing_rules() -> dict:
    """pricing_rules.yaml を読み込み（学習済みルールがあれば自動反映）"""
    rules_path = KNOWLEDGE_DIR / "pricing_rules.yaml"
    with open(rules_path, "r", encoding="utf-8") as f:
        rules = yaml.safe_load(f)
    try:
        from learning.apply_estimate import apply_learned_rules
        rules = apply_learned_rules(rules)
    except Exception:
        pass  # 学習ストア破損時も見積生成は止めない
    return rules


def load_item_templates() -> dict:
    """item_templates.yaml を読み込み"""
    templates_path = KNOWLEDGE_DIR / "item_templates.yaml"
    with open(templates_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)
