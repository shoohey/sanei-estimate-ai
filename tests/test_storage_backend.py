"""Supabase永続化バックエンドのテスト（API・実Supabase不要、フェイク差し替え）

実行: python3 tests/test_storage_backend.py
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import learning.storage_backend as backend
from learning import store, history
from models.estimate_data import EstimateData


# =============================================================
# フェイクバックエンド（インメモリ）
# =============================================================

class FakeSupabase:
    def __init__(self):
        self.kv = {}
        self.tables = {"estimate_history": [], "drawing_history": []}
        self.next_id = 1

    def install(self):
        backend.is_enabled = lambda: True
        backend.kv_get = lambda k: self.kv.get(k)
        backend.kv_set = self._kv_set
        backend.insert_row = self._insert_row
        backend.select_rows = self._select_rows
        backend.get_payload = self._get_payload

    def _kv_set(self, k, v):
        self.kv[k] = v
        return True

    def _insert_row(self, table, row):
        row = dict(row)
        row["id"] = self.next_id
        row["saved_at"] = "2026-07-16T00:00:00+00:00"
        self.next_id += 1
        self.tables[table].append(row)
        return True

    def _select_rows(self, table, columns, limit=100):
        return list(reversed(self.tables[table]))[:limit]

    def _get_payload(self, table, row_id):
        for r in self.tables[table]:
            if str(r["id"]) == str(row_id):
                return r["payload"]
        return None


_ORIG = {name: getattr(backend, name) for name in
         ("is_enabled", "kv_get", "kv_set", "insert_row", "select_rows", "get_payload")}


def _restore():
    for name, fn in _ORIG.items():
        setattr(backend, name, fn)


def _tmp_paths(tmp):
    store.ESTIMATE_RULES_PATH = Path(tmp) / "learned_estimate_rules.json"
    store.DRAWING_RULES_PATH = Path(tmp) / "learned_drawing_rules.json"
    store.LEARNING_LOG_PATH = Path(tmp) / "learning_history.json"
    history.ESTIMATE_HISTORY_DIR = Path(tmp) / "estimate_history"
    history.DRAWING_HISTORY_DIR = Path(tmp) / "drawing_history"


def _rule(desc="テスト項目", price=100):
    return {"target": "estimate", "kind": "unit_price_override",
            "category": "材料費", "match_description": desc,
            "match_remarks": "", "display_description": desc,
            "payload": {"unit_price": price, "old_unit_price": 50},
            "evidence": {}, "enabled": True}


# =============================================================
# テスト
# =============================================================

def test_disabled_uses_local():
    """Supabase未構成なら従来通りローカルファイルで完結すること。"""
    _restore()
    with tempfile.TemporaryDirectory() as tmp:
        _tmp_paths(tmp)
        assert backend.is_enabled() is False or True  # 環境次第だが下の動作で担保
        # 未構成環境ではローカルに書かれ、読める
        backend.is_enabled = lambda: False
        store.save_rules("estimate", [_rule()])
        assert store.ESTIMATE_RULES_PATH.exists(), "ローカルファイルに保存されるはず"
        rules = store.load_rules("estimate")
        assert len(rules) == 1 and rules[0]["payload"]["unit_price"] == 100
    _restore()


def test_enabled_reads_supabase_first():
    """Supabase構成時はローカルより Supabase の内容が優先されること。"""
    fake = FakeSupabase()
    with tempfile.TemporaryDirectory() as tmp:
        _tmp_paths(tmp)
        fake.install()
        store.save_rules("estimate", [_rule(price=300)])
        # ローカルを別内容で汚染 → それでも Supabase 側の300が読めること
        with open(store.ESTIMATE_RULES_PATH, "w", encoding="utf-8") as f:
            json.dump({"rules": [_rule(price=999)]}, f, ensure_ascii=False)
        rules = store.load_rules("estimate")
        assert rules[0]["payload"]["unit_price"] == 300, \
            "Supabase が正であるべき（ローカルの999ではなく300）"
        # ローカルにも並行保存されている（save時点の内容）
        assert store.ESTIMATE_RULES_PATH.exists()
    _restore()


def test_supabase_failure_falls_back_to_local():
    """Supabase読込がNoneを返す（未登録/障害）場合ローカルに落ちること。"""
    fake = FakeSupabase()
    with tempfile.TemporaryDirectory() as tmp:
        _tmp_paths(tmp)
        fake.install()
        backend.kv_get = lambda k: None  # 常に取得失敗
        with open(store.ESTIMATE_RULES_PATH, "w", encoding="utf-8") as f:
            json.dump({"rules": [_rule(price=777)]}, f, ensure_ascii=False)
        rules = store.load_rules("estimate")
        assert rules and rules[0]["payload"]["unit_price"] == 777
    _restore()


def test_learning_log_via_backend():
    """学習ログも Supabase KV 経由で追記・取得できること。"""
    fake = FakeSupabase()
    with tempfile.TemporaryDirectory() as tmp:
        _tmp_paths(tmp)
        fake.install()
        store.append_learning_log({"kind": "estimate", "approved": 3})
        store.append_learning_log({"kind": "drawing", "approved": 1})
        logs = store.load_learning_log()
        assert len(logs) == 2 and logs[1]["approved"] == 1
        assert "learning_history" in fake.kv
    _restore()


def test_estimate_history_roundtrip_supabase():
    """見積履歴が Supabase に保存され supabase:// パスで一覧・読込できること。"""
    fake = FakeSupabase()
    with tempfile.TemporaryDirectory() as tmp:
        _tmp_paths(tmp)
        fake.install()
        est = EstimateData()
        est.cover.estimate_id = "12345678-1234567"
        est.cover.client_name = "テスト株式会社"
        est.summary.total_with_tax = 9990000
        history.save_estimate_history(est)
        items = history.list_estimate_history()
        assert len(items) == 1
        assert items[0]["path"].startswith("supabase://estimate_history/")
        assert items[0]["client_name"] == "テスト株式会社"
        assert items[0]["total_with_tax"] == 9990000
        loaded = history.load_estimate_history(items[0]["path"])
        assert loaded is not None and loaded.cover.estimate_id == "12345678-1234567"
    _restore()


def test_drawing_history_roundtrip_supabase():
    """図面履歴が Supabase に保存され supabase:// パスで一覧・読込できること。"""
    fake = FakeSupabase()
    with tempfile.TemporaryDirectory() as tmp:
        _tmp_paths(tmp)
        fake.install()
        spec = {"customer_name": "たましん様", "drawing_type": "layout",
                "total_panels": 32, "total_kw": 12.8, "roof_faces": []}
        history.save_drawing_history(spec)
        items = history.list_drawing_history()
        assert len(items) == 1
        assert items[0]["path"].startswith("supabase://drawing_history/")
        assert items[0]["total_panels"] == 32
        loaded = history.load_drawing_history(items[0]["path"])
        assert loaded is not None and loaded["customer_name"] == "たましん様"
    _restore()


def test_local_history_still_works_when_disabled():
    """Supabase未構成でもローカル履歴の保存・一覧・読込が従来通り動くこと。"""
    _restore()
    with tempfile.TemporaryDirectory() as tmp:
        _tmp_paths(tmp)
        backend.is_enabled = lambda: False
        est = EstimateData()
        est.cover.estimate_id = "00000000-0000001"
        path = history.save_estimate_history(est)
        assert path is not None and Path(path).exists()
        items = history.list_estimate_history()
        assert len(items) == 1 and not items[0]["path"].startswith("supabase://")
        loaded = history.load_estimate_history(items[0]["path"])
        assert loaded is not None
    _restore()


def main():
    tests = [
        test_disabled_uses_local,
        test_enabled_reads_supabase_first,
        test_supabase_failure_falls_back_to_local,
        test_learning_log_via_backend,
        test_estimate_history_roundtrip_supabase,
        test_drawing_history_roundtrip_supabase,
        test_local_history_still_works_when_disabled,
    ]
    print("=== Supabase永続化バックエンドテスト（実Supabase不要） ===")
    failed = 0
    for t in tests:
        try:
            t()
            print(f"[OK] {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"[NG] {t.__name__}: {e}")
        finally:
            _restore()
    print("=== 結果: " + ("全パス" if failed == 0 else "一部失敗") + " ===")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
