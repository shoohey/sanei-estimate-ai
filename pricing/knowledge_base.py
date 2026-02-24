"""YAMLルール読み込み"""
import yaml
from pathlib import Path
from config import KNOWLEDGE_DIR


def load_pricing_rules() -> dict:
    """pricing_rules.yaml を読み込み"""
    rules_path = KNOWLEDGE_DIR / "pricing_rules.yaml"
    with open(rules_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_item_templates() -> dict:
    """item_templates.yaml を読み込み"""
    templates_path = KNOWLEDGE_DIR / "item_templates.yaml"
    with open(templates_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)
