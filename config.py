"""設定・定数管理"""
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# プロジェクトルート
BASE_DIR = Path(__file__).resolve().parent

# API設定（Streamlit Cloud secrets にも対応）
def get_api_key() -> str:
    """毎回呼び出し時にAPIキーを取得する"""
    # 1. Streamlit Cloud secrets
    try:
        import streamlit as st
        key = st.secrets.get("ANTHROPIC_API_KEY", "")
        if key:
            return key
    except Exception:
        pass
    # 2. 環境変数 / .env
    return os.getenv("ANTHROPIC_API_KEY", "")

CLAUDE_MODEL = "claude-sonnet-4-20250514"

# パス定数
ASSETS_DIR = BASE_DIR / "assets"
FONTS_DIR = ASSETS_DIR / "fonts"
KNOWLEDGE_DIR = BASE_DIR / "knowledge"
SAMPLE_DIR = BASE_DIR / "sample"

# フォントパス
FONT_REGULAR = FONTS_DIR / "NotoSansJP-Regular.ttf"
FONT_BOLD = FONTS_DIR / "NotoSansJP-Bold.ttf"

# 会社情報
COMPANY_INFO = {
    "name": "株式会社サンエー",
    "name_short": "サンエー",
    "postal_code": "〒238-0014",
    "address": "神奈川県横須賀市三春町2-10",
    "tel": "TEL 046-828-3351",
    "fax": "FAX 046-828-3352",
    "slogan": "未来の当たり前を、いちはやく",
    "default_representative": "根本　雄介",
}

# PDF設定
PDF_PAGE_SIZE = "A4"
TAX_RATE = 0.10  # 消費税率10%

# 見積ID生成
import random
import time

def generate_estimate_id() -> str:
    """見積IDを生成（サンプル形式: 22730522-4367674）"""
    part1 = random.randint(10000000, 99999999)
    part2 = random.randint(1000000, 9999999)
    return f"{part1}-{part2}"
