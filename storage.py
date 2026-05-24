import json
import os
import threading
from astrbot.core import sp

SP_PLAN_MAPPING = "afdian_model:plan_mapping"
SP_ACTIVE_UMOS = "afdian_model:active_umos"
SP_UMO_PREFIX = "afdian_model:umo:"
SP_BY_AFDIAN = "afdian_model:by_afdian:"
SP_USER_INDEX = "afdian_model:user_index"


class StorageManager:
    def __init__(self, data_dir: str, wire_fn=None):
        self._wire = wire_fn or print
        self._data_dir = data_dir
        self._orders_path = os.path.join(data_dir, "processed_orders.json")
        self._persistence_path = os.path.join(data_dir, "persistence.json")
        self._processed_orders = self._load_processed_orders()
        self._lock = threading.Lock()
        self._batch_depth = 0

    def _load_processed_orders(self) -> set:
        try:
            if os.path.exists(self._orders_path):
                with open(self._orders_path, "r", encoding="utf-8") as f:
                    return set(json.load(f))
        except Exception:
            pass
        return set()

    def _save_processed_orders(self):
        with self._lock:
            try:
                os.makedirs(self._data_dir, exist_ok=True)
                tmp_path = self._orders_path + ".tmp"
                with open(tmp_path, "w", encoding="utf-8") as f:
                    json.dump(list(self._processed_orders), f)
                os.replace(tmp_path, self._orders_path)
            except Exception as e:
                self._wire(f"[AfdianModel] 保存订单记录失败: {e}", "error")

    def _save_processed_orders_nolock(self):
        """内部使用（调用方已持有 _lock）"""
        try:
            os.makedirs(self._data_dir, exist_ok=True)
            tmp_path = self._orders_path + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(list(self._processed_orders), f)
            os.replace(tmp_path, self._orders_path)
        except Exception as e:
            self._wire(f"[AfdianModel] 保存订单记录失败: {e}", "error")

    def is_order_processed(self, order_no: str) -> bool:
        return order_no in self._processed_orders

    def mark_order_processed(self, order_no: str):
        with self._lock:
            self._processed_orders.add(order_no)
            self._save_processed_orders_nolock()

    def unmark_order_processed(self, order_no: str):
        with self._lock:
            self._processed_orders.discard(order_no)
            self._save_processed_orders_nolock()

    def clear_orders(self):
        with self._lock:
            self._processed_orders.clear()
            self._save_processed_orders_nolock()
            try:
                if os.path.exists(self._orders_path):
                    os.remove(self._orders_path)
            except Exception:
                pass

    def full_reset(self) -> dict:
        """完全清除所有存储数据（订单记录 + 用户数据 + 映射关系）。
        返回清除统计。"""
        stats = {"orders": 0, "umo_data": 0, "user_mappings": 0, "active_umos": 0}

        # 1. 清除已处理订单
        stats["orders"] = len(self._processed_orders)
        self.clear_orders()

        # 2. 清除所有 umo 数据（从 active_umos 遍历 + user_index 反查兜底）
        active = list(sp.get(SP_ACTIVE_UMOS, []))
        stats["active_umos"] = len(active)
        seen_umo = set()

        # 活跃绑定中的 umo_keys
        for umo_key in active:
            sp.put(umo_key, None)
            sp.put(umo_key + ":current", None)
            seen_umo.add(umo_key)
            stats["umo_data"] += 1

        # 用户索引中反查的非活跃 umo_keys（如已降级 Lv0 的残留数据）
        user_index = list(sp.get(SP_USER_INDEX, []))
        for user_id in user_index:
            umo_key = sp.get(f"{SP_BY_AFDIAN}{user_id}", None)
            if umo_key and umo_key not in seen_umo:
                sp.put(umo_key, None)
                sp.put(umo_key + ":current", None)
                stats["umo_data"] += 1
            sp.put(f"{SP_BY_AFDIAN}{user_id}", None)
            stats["user_mappings"] += 1

        # 3. 清除主键
        sp.put(SP_ACTIVE_UMOS, [])
        sp.put(SP_PLAN_MAPPING, {})
        sp.put(SP_USER_INDEX, [])

        # 4. 删除持久化文件
        for path in (self._persistence_path,):
            try:
                if os.path.exists(path):
                    os.remove(path)
            except Exception:
                pass

        self._wire(f"[AfdianModel] 全量重置完成: {stats}", "info")
        return stats

    @staticmethod
    def _umo_key(umo) -> str:
        return f"{SP_UMO_PREFIX}{json.dumps(umo, separators=(',', ':'), sort_keys=True)}"

    def get_umo_data(self, umo) -> dict:
        key = self._umo_key(umo)
        data = sp.get(key, {})
        if data:
            data = dict(data)
            data["prefixes"] = self._str_to_list(data.get("prefixes", ""))
        return data

    def set_umo_data(self, umo, data: dict):
        key = self._umo_key(umo)
        sp.put(key, data)
        if not self._batch_depth:
            self._dump_state()

    def get_umo_data_by_key(self, umo_key: str) -> dict:
        """直接通过 umo_key 获取 umo 数据，避免 key→umo→key 的来回转换"""
        data = sp.get(umo_key, {})
        if data:
            data = dict(data)
            data["prefixes"] = self._str_to_list(data.get("prefixes", ""))
        return data

    def set_umo_data_by_key(self, umo_key: str, data: dict):
        """直接通过 umo_key 写入数据并触发持久化"""
        sp.put(umo_key, data)
        if not self._batch_depth:
            self._dump_state()

    def remove_umo_by_key(self, umo_key: str):
        """清理 umo 数据及 :current 键并触发持久化"""
        sp.put(umo_key, None)
        sp.put(umo_key + ":current", None)
        if not self._batch_depth:
            self._dump_state()

    def register_umo(self, umo_key: str):
        active = sp.get(SP_ACTIVE_UMOS, [])
        if umo_key not in active:
            active.append(umo_key)
            sp.put(SP_ACTIVE_UMOS, active)
        if not self._batch_depth:
            self._dump_state()

    def unregister_umo(self, umo_key: str):
        active = sp.get(SP_ACTIVE_UMOS, [])
        if umo_key in active:
            active.remove(umo_key)
            sp.put(SP_ACTIVE_UMOS, active)
        if not self._batch_depth:
            self._dump_state()

    def get_active_umos(self) -> list:
        return sp.get(SP_ACTIVE_UMOS, [])

    def set_active_umos(self, active: list):
        sp.put(SP_ACTIVE_UMOS, active)
        if not self._batch_depth:
            self._dump_state()

    def get_plan_mapping(self) -> dict:
        return sp.get(SP_PLAN_MAPPING, {})

    def set_plan_mapping(self, mapping: dict):
        sp.put(SP_PLAN_MAPPING, mapping)
        if not self._batch_depth:
            self._dump_state()

    def get_user_mapping(self, user_id: str):
        return sp.get(f"{SP_BY_AFDIAN}{user_id}", None)

    def set_user_mapping(self, user_id: str, umo_key: str):
        sp.put(f"{SP_BY_AFDIAN}{user_id}", umo_key)
        self._register_user_id(user_id)
        if not self._batch_depth:
            self._dump_state()

    def remove_user_mapping(self, user_id: str):
        sp.put(f"{SP_BY_AFDIAN}{user_id}", None)
        self._unregister_user_id(user_id)
        if not self._batch_depth:
            self._dump_state()

    @staticmethod
    def _default_model() -> str:
        """AstrBot 配置文件中的全局默认模型名（scope=global）"""
        return sp.get("curr_provider", "", scope="global", scope_id="global") or "默认模型"

    def get_current_model(self, umo) -> str:
        return sp.get(self._umo_key(umo) + ":current", self._default_model())

    def set_current_model(self, umo, model: str):
        sp.put(self._umo_key(umo) + ":current", model)
        if not self._batch_depth:
            self._dump_state()

    def set_current_model_by_key(self, umo_key: str, model: str):
        """直接通过 umo_key 设置当前模型并触发持久化"""
        sp.put(umo_key + ":current", model)
        if not self._batch_depth:
            self._dump_state()

    @staticmethod
    def _list_to_str(lst: list) -> str:
        if not lst:
            return ""
        return ",".join(lst)

    @staticmethod
    def _str_to_list(s: str) -> list:
        if not s or not isinstance(s, str):
            return []
        return [item.strip() for item in s.split(",") if item.strip()]

    # ── 持久化层 ──

    def _register_user_id(self, user_id: str):
        """记录 user_id 以便 dump_state 时能枚举 user_mappings"""
        idx = sp.get(SP_USER_INDEX, [])
        if user_id not in idx:
            idx.append(user_id)
            sp.put(SP_USER_INDEX, idx)

    def _unregister_user_id(self, user_id: str):
        idx = sp.get(SP_USER_INDEX, [])
        if user_id in idx:
            idx.remove(user_id)
            sp.put(SP_USER_INDEX, idx)

    def _dump_state(self):
        """将所有 sp 状态写入 persistence.json（调用方需确保持有 _lock）"""
        with self._lock:
            try:
                state = {
                    "plan_mapping": sp.get(SP_PLAN_MAPPING, {}),
                    "active_umos": sp.get(SP_ACTIVE_UMOS, []),
                    "umo_data": {},
                    "user_mappings": {},
                    "user_index": sp.get(SP_USER_INDEX, []),
                }
                for umo_key in state["active_umos"]:
                    data = sp.get(umo_key, {})
                    if data:
                        state["umo_data"][umo_key] = dict(data) if isinstance(data, dict) else data
                    current = sp.get(umo_key + ":current", "")
                    if current:
                        state["umo_data"][umo_key + ":current"] = current
                for user_id in state["user_index"]:
                    val = sp.get(f"{SP_BY_AFDIAN}{user_id}", None)
                    if val is not None:
                        state["user_mappings"][user_id] = val

                os.makedirs(self._data_dir, exist_ok=True)
                tmp_path = self._persistence_path + ".tmp"
                with open(tmp_path, "w", encoding="utf-8") as f:
                    json.dump(state, f, ensure_ascii=False, indent=2)
                os.replace(tmp_path, self._persistence_path)
            except Exception as e:
                self._wire(f"[AfdianModel] 状态持久化失败: {e}", "error")

    def restore_state(self):
        """从 persistence.json 恢复所有 sp 状态（插件重载后调用）"""
        try:
            if not os.path.exists(self._persistence_path):
                self._wire("[AfdianModel] 无持久化文件，跳过恢复", "info")
                return False
            with open(self._persistence_path, "r", encoding="utf-8") as f:
                state = json.load(f)

            restored = 0
            if state.get("plan_mapping"):
                sp.put(SP_PLAN_MAPPING, state["plan_mapping"])
                restored += 1
            if state.get("active_umos"):
                sp.put(SP_ACTIVE_UMOS, state["active_umos"])
                restored += 1
            if state.get("user_index"):
                sp.put(SP_USER_INDEX, state["user_index"])
            for key, value in state.get("umo_data", {}).items():
                if value:
                    sp.put(key, value)
            for user_id, umo_key in state.get("user_mappings", {}).items():
                sp.put(f"{SP_BY_AFDIAN}{user_id}", umo_key)

            active_count = len(state.get("active_umos", []))
            self._wire(
                f"[AfdianModel] ✅ 状态从持久化文件恢复: {active_count} 个活跃绑定",
                "info"
            )
            return True
        except Exception as e:
            self._wire(f"[AfdianModel] 状态恢复失败: {e}", "error")
            return False

    def begin_batch(self):
        """开始批量写入：挂起 _dump_state 直到 end_batch"""
        self._batch_depth += 1

    def end_batch(self):
        """结束批量写入：若计数归零则执行一次 _dump_state"""
        if self._batch_depth > 0:
            self._batch_depth -= 1
        if self._batch_depth == 0:
            self._dump_state()

    def persist(self):
        """公开的持久化入口，供直接 sp.put 后调用"""
        self._dump_state()

    @staticmethod
    def migrate_umo_data(data: dict, wire_fn=None) -> dict:
        """迁移旧数据到新的分级存储格式（l1_days/l2_days/active_level）。
        幂等操作，已迁移的数据不会被重复处理。"""
        if not data or not isinstance(data, dict):
            return data
        if "l1_days" not in data and "l2_days" not in data:
            old_level = data.get("level", "1")
            old_days = data.get("remaining_days", 0)
            if old_level == "2":
                data["l2_days"] = old_days
                data["l1_days"] = 0
            else:
                data["l1_days"] = old_days
                data["l2_days"] = 0
            data["active_level"] = old_level
            if wire_fn:
                wire_fn(f"[AfdianModel] 数据迁移: lv={old_level} days={old_days} -> l1={data['l1_days']} l2={data['l2_days']}")
        if "active_level" not in data:
            if data.get("l2_days", 0) > 0:
                data["active_level"] = "2"
            else:
                data["active_level"] = data.get("level", "1")
        data["remaining_days"] = data.get("l1_days", 0) + data.get("l2_days", 0)
        return data
