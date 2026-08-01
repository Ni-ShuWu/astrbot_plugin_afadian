"""服务容器 —— 替代层层参数传递，所有模块通过 Services 获取依赖。"""

from typing import Callable

from .plan_manager import PlanManager
from .storage import StorageManager
from .user_manager import UserManager
from .utils import LogFn


class Services:
    """插件全局服务容器。

    所有命令模块通过此容器访问依赖，无需在构造函数中逐层传递参数。
    配置以 AstrBot 官方 AstrBotConfig 为唯一数据源：
    - config_fn()：返回当前配置（dict）
    - save_config()：把当前配置保存到官方配置文件（data/config/<插件名>_config.json）
    """

    __slots__ = (
        "api_getter", "config_fn", "save_config", "storage",
        "plan_manager", "user_manager", "wire", "astrbot_context",
    )

    def __init__(
        self,
        *,
        api_getter: Callable,
        config_fn: Callable,
        save_config: Callable,
        storage: StorageManager,
        plan_manager: PlanManager,
        user_manager: UserManager,
        wire: LogFn,
        astrbot_context=None,
    ) -> None:
        self.api_getter = api_getter
        self.config_fn = config_fn
        self.save_config = save_config
        self.storage = storage
        self.plan_manager = plan_manager
        self.user_manager = user_manager
        self.wire = wire
        self.astrbot_context = astrbot_context  # AstrBot Context 对象

    async def migrate_legacy_config(self) -> int:
        """一次性迁移：把旧 sp 配置副本写入官方配置（仅补空值）。返回迁移的配置项数量。"""
        from .storage import SP_PLUGIN_CONFIG, sp_get

        cfg = self.config_fn()
        if not cfg:
            return 0
        old = await sp_get(SP_PLUGIN_CONFIG, {})
        if not isinstance(old, dict) or not old:
            return 0
        merged = 0
        for k, v in old.items():
            if k not in cfg or not cfg.get(k):
                cfg[k] = v
                merged += 1
        if merged:
            self.save_config()
        if merged:
            self.wire(f"旧配置迁移完成: {merged} 个配置项已写入官方配置")
        return merged
