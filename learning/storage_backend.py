"""学習データの保存バックエンド（Supabase / ローカルファイル自動切替）

SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY が設定されていれば Supabase
（PostgREST）に永続化し、無ければ従来通りローカルファイルのみに保存する。
Streamlit Cloud のコンテナは再起動で実行時ファイルが消えるため、
本番の学習データ永続化には Supabase 設定が必須。

設計方針:
- Supabase 有効時は Supabase が正。ローカルファイルにも並行保存する
  （オフライン時のフォールバック兼バックアップ）。
- Supabase 障害時は警告ログのみでローカル動作に落ち、本体フローを止めない。
"""
import logging
import os
from typing import Optional

import requests

logger = logging.getLogger(__name__)

_TIMEOUT = 10  # 秒。UI操作を待たせすぎない


def _creds() -> tuple[str, str]:
    """Supabase 接続情報を st.secrets → 環境変数 → .env.local の順で取得する。"""
    # pytest 実行中・テストフラグ設定時は Supabase を常に無効扱いにする。
    # ローカルの .env.local に実クレデンシャルがあると、テストが本番の
    # 学習データ（app_storage 等）を読み書き・消去してしまうため。
    # テストは FakeSupabase（tests/test_storage_backend.py）でモジュール属性を
    # 差し替える方式なので、このガードの影響を受けない。
    #
    # SANEI_DISABLE_SUPABASE: スクリプト式実行（python3 tests/xxx.py）では
    # PYTEST_CURRENT_TEST が立たず、2026-08-10 にテスト実行が本番の共有
    # 学習データ（learned_estimate_rules 等）を上書き・消去する事故が実発生した。
    # store を触るテスト・検証スクリプトは import 前にこの環境変数を必ず立てること。
    if "PYTEST_CURRENT_TEST" in os.environ \
            or os.environ.get("SANEI_DISABLE_SUPABASE"):
        return "", ""
    url = key = ""
    try:
        import streamlit as st
        url = st.secrets.get("SUPABASE_URL", "")
        key = st.secrets.get("SUPABASE_SERVICE_ROLE_KEY", "")
    except Exception:
        pass
    url = url or os.getenv("SUPABASE_URL", "")
    key = key or os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
    if not (url and key):
        # ローカル開発用: .env.local（supabase-engineer が生成、git管理外）
        try:
            from dotenv import load_dotenv
            from config import BASE_DIR
            load_dotenv(BASE_DIR / ".env.local")
            url = url or os.getenv("SUPABASE_URL", "")
            key = key or os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
        except Exception:
            pass
    return url.rstrip("/"), key


def is_enabled() -> bool:
    """Supabase 永続化が構成されているか。"""
    url, key = _creds()
    return bool(url and key)


def _headers(key: str) -> dict:
    return {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }


# =============================================================
# KVドキュメント（app_storage: 従来のJSONファイル1個 = 1行）
# =============================================================

def kv_get(storage_key: str) -> Optional[dict]:
    """app_storage から value(jsonb) を取得する。未登録/失敗は None。"""
    url, key = _creds()
    if not (url and key):
        return None
    try:
        r = requests.get(
            f"{url}/rest/v1/app_storage",
            params={"key": f"eq.{storage_key}", "select": "value"},
            headers=_headers(key), timeout=_TIMEOUT)
        r.raise_for_status()
        rows = r.json()
        if rows and isinstance(rows[0].get("value"), dict):
            return rows[0]["value"]
        return None
    except Exception as e:
        logger.warning("Supabase kv_get(%s) 失敗: %s", storage_key, e)
        return None


def kv_set(storage_key: str, value: dict) -> bool:
    """app_storage に upsert する。成功で True。"""
    url, key = _creds()
    if not (url and key):
        return False
    try:
        headers = _headers(key)
        headers["Prefer"] = "resolution=merge-duplicates"
        r = requests.post(
            f"{url}/rest/v1/app_storage",
            json={"key": storage_key, "value": value},
            headers=headers, timeout=_TIMEOUT)
        r.raise_for_status()
        return True
    except Exception as e:
        logger.warning("Supabase kv_set(%s) 失敗: %s", storage_key, e)
        return False


# =============================================================
# 履歴テーブル（estimate_history / drawing_history）
# =============================================================

def insert_row(table: str, row: dict) -> bool:
    """履歴テーブルに1行 insert する。成功で True。"""
    url, key = _creds()
    if not (url and key):
        return False
    try:
        r = requests.post(
            f"{url}/rest/v1/{table}",
            json=row, headers=_headers(key), timeout=_TIMEOUT)
        r.raise_for_status()
        return True
    except Exception as e:
        logger.warning("Supabase insert_row(%s) 失敗: %s", table, e)
        return False


def select_rows(table: str, columns: str, limit: int = 100) -> list[dict]:
    """履歴テーブルを saved_at 降順で取得する。失敗は []。"""
    url, key = _creds()
    if not (url and key):
        return []
    try:
        r = requests.get(
            f"{url}/rest/v1/{table}",
            params={"select": columns, "order": "saved_at.desc",
                    "limit": str(limit)},
            headers=_headers(key), timeout=_TIMEOUT)
        r.raise_for_status()
        rows = r.json()
        return rows if isinstance(rows, list) else []
    except Exception as e:
        logger.warning("Supabase select_rows(%s) 失敗: %s", table, e)
        return []


def get_payload(table: str, row_id: str) -> Optional[dict]:
    """履歴テーブルの1行の payload(jsonb) を取得する。"""
    url, key = _creds()
    if not (url and key):
        return None
    try:
        r = requests.get(
            f"{url}/rest/v1/{table}",
            params={"id": f"eq.{row_id}", "select": "payload"},
            headers=_headers(key), timeout=_TIMEOUT)
        r.raise_for_status()
        rows = r.json()
        if rows and isinstance(rows[0].get("payload"), dict):
            return rows[0]["payload"]
        return None
    except Exception as e:
        logger.warning("Supabase get_payload(%s, %s) 失敗: %s", table, row_id, e)
        return None
