"""AstrBot 爱发电模型订阅插件入口。

持久化基于 AstrBot sp（SQLite）异步接口；配置以官方 AstrBotConfig 为唯一数据源。
"""

import asyncio
import logging
import os
from logging.handlers import RotatingFileHandler

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

from .afdian_api import AfdianAPI
from .cron_tasks import CronTasks
from .plan_manager import PlanManager
from .services import Services
from .storage import StorageManager
from .user_manager import UserManager
from .utils import PLUGIN_LOG_PREFIX

PLUGIN_NAME = "astrbot_plugin_afdian_model"
PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(get_astrbot_data_path(), "plugin_data", PLUGIN_NAME)
PLUGIN_LOG_PATH = os.path.join(LOG_DIR, "plugin.log")

_plugin_logger: logging.Logger | None = None


def _get_plugin_logger() -> logging.Logger:
    """模块级单例日志器，避免热重载时重复注册文件 handler。"""
    global _plugin_logger
    if _plugin_logger is not None:
        return _plugin_logger
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        plog = logging.getLogger("afdian_model")
        if not plog.handlers:
            fh = RotatingFileHandler(
                PLUGIN_LOG_PATH, maxBytes=2 * 1024 * 1024, backupCount=5, encoding="utf-8"
            )
            fh.setLevel(logging.DEBUG)
            fh.setFormatter(
                logging.Formatter(
                    "[%(asctime)s] [%(levelname)s] %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S",
                )
            )
            plog.addHandler(fh)
        plog.setLevel(logging.DEBUG)
        plog.propagate = False
        _plugin_logger = plog
        return plog
    except OSError as e:
        logger.error(f"初始化插件日志失败: {e}")
        return logger  # type: ignore[return-value]


class AfdianModelPlugin(Star):
    """爱发电赞助模型订阅插件。"""

    def __init__(self, context: Context, config: AstrBotConfig = None) -> None:
        super().__init__(context)
        self._star_config = config if isinstance(config, dict) else {}
        self._api: AfdianAPI | None = None
        self._plog = _get_plugin_logger()
        self._svc = self._build_services()
        self._cron = CronTasks(self._svc)
        self._tasks: list[asyncio.Task] = [
            asyncio.create_task(self._startup()),
            asyncio.create_task(self._cron.cron_daily()),
            asyncio.create_task(self._cron.cron_poll()),
        ]

    async def _startup(self) -> None:
        """启动任务：同步方案映射 + 一次性迁移旧配置。"""
        try:
            await self._svc.plan_manager.sync_plan_mapping()
            await self._svc.migrate_legacy_config()
        except Exception as e:
            self._wire(f"启动任务异常: {e}", "error")

    async def terminate(self) -> None:
        """插件卸载/重载时取消定时任务并关闭 API 会话，防止多实例并发。"""
        for t in self._tasks:
            t.cancel()
        self._tasks.clear()
        if self._api is not None:
            try:
                await self._api.close()
            except Exception as e:
                self._wire(f"关闭爱发电 API 会话异常: {e}", "warning")
            self._api = None

    def _build_services(self) -> Services:
        storage = StorageManager(self._wire)
        plan_mgr = PlanManager(self._config, storage, self._wire)
        user_mgr = UserManager(storage, plan_mgr, self._wire)
        return Services(
            api_getter=self._get_api,
            config_fn=self._config,
            save_config=self.save_config,
            storage=storage,
            plan_manager=plan_mgr,
            user_manager=user_mgr,
            wire=self._wire,
            astrbot_context=self.context,
        )

    # ── 日志 ──────────────────────────────────────

    def _wire(self, msg: str, level: str = "info") -> None:
        text = f"{PLUGIN_LOG_PREFIX} {msg}"
        getattr(logger, level, logger.info)(text)
        getattr(self._plog, level, self._plog.info)(msg)

    # ── 配置（官方 AstrBotConfig 单源） ──────────────

    def _config(self) -> dict:
        return self._star_config

    def save_config(self) -> None:
        """把当前配置保存到官方配置文件。"""
        cfg = self._star_config
        saver = getattr(cfg, "save_config", None)
        if callable(saver):
            saver()
            self._wire("配置已保存到官方配置文件")
        else:
            self._wire("无官方配置对象，配置仅保存在内存", "warning")

    # ── 爱发电 API ────────────────────────────────

    def _get_api(self) -> AfdianAPI | None:
        cfg = self._config()
        uid = str(cfg.get("afdian_user_id", "") or "").strip()
        token = str(cfg.get("afdian_token", "") or "").strip()
        if not uid or not token:
            self._wire("爱发电 API 未配置（afdian_user_id / afdian_token）", "warning")
            return None
        if self._api is None:
            try:
                self._api = AfdianAPI(
                    uid, token, str(cfg.get("afdian_api_base", "https://afdian.net")), self._wire
                )
            except ValueError as e:
                self._wire(f"API 初始化失败: {e}", "error")
                return None
        return self._api

    # ── 权限 ──────────────────────────────────────

    async def _check_admin(self, event: AstrMessageEvent) -> bool:
        """管理员校验：优先走 Context 公开 API，兼容旧版本回退私有属性。"""
        try:
            getter = getattr(self.context, "get_config", None)
            cfg = getter() if callable(getter) else getattr(self.context, "_config", {})
            admins = (cfg or {}).get("admins_id", []) or []
            return str(event.get_sender_id()) in [str(a) for a in admins]
        except Exception:
            return False

    # ── 指令注册（懒导入避免循环依赖） ──────────────

    @filter.command("afdian_help")
    async def cmd_help(self, event: AstrMessageEvent):
        from .commands_user import cmd_help
        async for res in cmd_help(self._svc, event):
            yield res

    @filter.command("afdian_bind")
    async def cmd_bind(self, event: AstrMessageEvent):
        from .commands_user import cmd_bind
        async for res in cmd_bind(self._svc, event):
            yield res

    @filter.command("afdian_models")
    async def cmd_models(self, event: AstrMessageEvent):
        from .commands_user import cmd_models
        async for res in cmd_models(self._svc, event):
            yield res

    @filter.command("afdian_switch")
    async def cmd_switch(self, event: AstrMessageEvent):
        from .commands_user import cmd_switch
        async for res in cmd_switch(self._svc, event, self._check_admin):
            yield res

    @filter.command("afdian_status")
    async def cmd_status(self, event: AstrMessageEvent):
        from .commands_user import cmd_status
        async for res in cmd_status(self._svc, event):
            yield res

    @filter.command("afdian_reset")
    async def cmd_reset(self, event: AstrMessageEvent):
        from .commands_admin import cmd_reset
        async for res in cmd_reset(self._svc, event, self._check_admin):
            yield res

    @filter.command("afdian_reset_all")
    async def cmd_reset_all(self, event: AstrMessageEvent):
        from .commands_admin import cmd_reset_all
        async for res in cmd_reset_all(self._svc, event, self._check_admin):
            yield res

    @filter.command("afdian_addmodels")
    async def cmd_addmodels(self, event: AstrMessageEvent):
        from .commands_model import cmd_addmodels
        async for res in cmd_addmodels(self._svc, event, self._check_admin):
            yield res

    @filter.command("afdian_delmodels")
    async def cmd_delmodels(self, event: AstrMessageEvent):
        from .commands_model import cmd_delmodels
        async for res in cmd_delmodels(self._svc, event, self._check_admin):
            yield res

    @filter.command("afdian_addplan")
    async def cmd_addplan(self, event: AstrMessageEvent):
        from .commands_plan import cmd_addplan
        async for res in cmd_addplan(self._svc, event, self._check_admin):
            yield res

    @filter.command("afdian_delplan")
    async def cmd_delplan(self, event: AstrMessageEvent):
        from .commands_plan import cmd_delplan
        async for res in cmd_delplan(self._svc, event, self._check_admin):
            yield res

    @filter.command("afdian_query")
    async def cmd_query(self, event: AstrMessageEvent):
        from .commands_admin import cmd_query
        async for res in cmd_query(self._svc, event, self._check_admin):
            yield res

    @filter.command("afdian_getconfig")
    async def cmd_getconfig(self, event: AstrMessageEvent):
        from .commands_admin import cmd_getconfig
        async for res in cmd_getconfig(self._svc, event, self._check_admin):
            yield res

    @filter.command("afdian_setconfig")
    async def cmd_setconfig(self, event: AstrMessageEvent):
        from .commands_admin import cmd_setconfig
        async for res in cmd_setconfig(self._svc, event, self._check_admin):
            yield res

    @filter.command("afdian_migrateconfig")
    async def cmd_migrateconfig(self, event: AstrMessageEvent):
        from .commands_admin import cmd_migrateconfig
        async for res in cmd_migrateconfig(self._svc, event, self._check_admin):
            yield res
