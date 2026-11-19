import random
import threading
from typing import Iterable, List, Optional, Tuple
import config as app_config
from runtime_state import app_state


class ExpressKeyManager:
    """管理 Agent Platform (原 Vertex AI) Express Mode API Key，支持随机或轮询选择。

    多账号支持：每个 Key 是一条记录（Key + 它自己的 GCP Project ID），
    列表持久化在 web_state.json，控制台可随时增删改（热生效，无需重启）。
    环境变量 VERTEX_EXPRESS_API_KEY 只在**首次启动**时做一次性导入。
    """

    def __init__(self):
        self._lock = threading.Lock()
        self.round_robin_index: int = 0

    # ---------- 读取 ----------

    def get_records(self) -> List[dict]:
        """当前启用的 Key 记录（每次调用直接读内存态，控制台改完立即生效）。"""
        return [r for r in app_state.get_express_keys() if r.get("enabled", True)]

    @property
    def express_keys(self) -> List[str]:
        return [r["key"] for r in self.get_records()]

    def get_total_keys(self) -> int:
        return len(self.get_records())

    # ---------- 选择 ----------

    def get_random_express_record(self, exclude_keys: Optional[Iterable[str]] = None) -> Optional[dict]:
        excluded = set(exclude_keys) if exclude_keys else set()
        with self._lock:
            records = self.get_records()
            if not records:
                print("❌ [密钥配置] 未配置任何 Express API Key，无法调用 Gemini Express Mode。")
                return None
            indexed = [(i, rec) for i, rec in enumerate(records) if rec["key"] not in excluded]
            if not indexed:
                return None
            random.shuffle(indexed)
            original_idx, rec = indexed[0]
        return dict(rec, index=original_idx)

    def get_roundrobin_express_record(self, exclude_keys: Optional[Iterable[str]] = None) -> Optional[dict]:
        excluded = set(exclude_keys) if exclude_keys else set()
        with self._lock:
            records = self.get_records()
            if not records:
                print("❌ [密钥配置] 未配置任何 Express API Key，无法调用 Gemini Express Mode。")
                return None
            total = len(records)
            if self.round_robin_index >= total:
                self.round_robin_index = 0
            start = self.round_robin_index
            chosen = None
            for offset in range(total):
                idx = (start + offset) % total
                rec = records[idx]
                if rec["key"] not in excluded:
                    self.round_robin_index = (idx + 1) % total
                    chosen = (idx, rec)
                    break
            if chosen is None:
                return None
            original_idx, rec = chosen
        return dict(rec, index=original_idx)

    def get_express_record(self, exclude_keys: Optional[Iterable[str]] = None) -> Optional[dict]:
        """按控制台策略选一条 Key 记录（含它自己的 Project ID）。"""
        if app_state.get_setting("roundrobin", app_config.ROUNDROBIN):
            return self.get_roundrobin_express_record(exclude_keys=exclude_keys)
        return self.get_random_express_record(exclude_keys=exclude_keys)

    # ---------- 向后兼容接口（只要 (序号, Key)） ----------

    def get_random_express_key(self) -> Optional[Tuple[int, str]]:
        rec = self.get_random_express_record()
        return (rec["index"], rec["key"]) if rec else None

    def get_roundrobin_express_key(self) -> Optional[Tuple[int, str]]:
        rec = self.get_roundrobin_express_record()
        return (rec["index"], rec["key"]) if rec else None

    def get_express_api_key(self) -> Optional[Tuple[int, str]]:
        rec = self.get_express_record()
        return (rec["index"], rec["key"]) if rec else None

    def get_all_keys_indexed(self) -> List[Tuple[int, str]]:
        return list(enumerate(self.express_keys))

    def refresh_keys(self):
        with self._lock:
            self.round_robin_index = 0
        print(f"🔄 [密钥刷新] 当前共 {self.get_total_keys()} 个可用 Express API Key。")
