"""服务容器 —— 替代层层参数传递，所有模块通过 Services 获取依赖。"""

from typing import Protocol

from astrbot.core import sp as _sp

from .config import ConfigManager
from .plan_manager import PlanManager
from .storage import StorageManager
from .user_manager import UserManager
from .utils import LogFn


class ApiGetter(Protocol):
    def __call__(self) -> "AfdianAPI | None": ...


class ConfigFn(Protocol):
    def __call__(self) -> dict: ...


class Services:
    """插件全局服务容器。

    所有命令模块通过此容器访问依赖，无需在构造函数中逐层传递 7 个参数。
    """

    __slots__ = (
        "api_getter", "config_fn", "config_manager",
        "storage", "plan_manager", "user_manager", "wire",
    )

    def __init__(
        self,
        *,
        api_getter: ApiGetter,
        config_fn: ConfigFn,
        config_manager: ConfigManager,
        storage: StorageManager,
        plan_manager: PlanManager,
        user_manager: UserManager,
        wire: LogFn,
    ) -> None:
        self.api_getter = api_getter
        self.config_fn = config_fn
        self.config_manager = config_manager
        self.storage = storage
        self.plan_manager = plan_manager
        self.user_manager = user_manager
        self.wire = wire

    @property
    def sp(self):
        """便捷访问 AstrBot SharedPreferences。"""
        return _sp
