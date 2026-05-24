"""AstrBot 爱发电模型订阅插件入口。

架构：AfdianModelPlugin(Star) → Services 容器 → 各命令/管理模块。
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
from .config import ConfigManager
from .cron_tasks import CronTasks
from .plan_manager import PlanManager
from .services import Services
from .storage import StorageManager
from .user_manager import UserManager
from .utils import PLUGIN_LOG_PREFIX, LogFn, log_msg

PLUGIN_NAME = "astrbot_plugin_afdian_model"
PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
FRAMEWORK_DATA_DIR = os.path.join(get_astrbot_data_path(), "plugin_data", PLUGIN_NAME)
DATA_DIR = FRAMEWORK_DATA_DIR
PLUGIN_LOG_PATH = os.path.join(FRAMEWORK_DATA_DIR, "plugin.log")


class AfdianModelPlugin(Star):
    """爱发电赞助模型订阅插件。"""

    def __init__(self, context: Context, config: AstrBotConfig = None) -> None:
        super().__init__(context)
        self._star_config = config if config else {}
        self._api: AfdianAPI | None = None

        # 日志
        self._plog = self._init_plugin_logger()

        # 服务容器
        svc = self._build_services()
        self._svc = svc

        # 定时任务
        self._cron = CronTasks(svc)
        asyncio.create_task(self._cron.cron_daily())
        asyncio.create_task(self._cron.cron_poll())

    # ── 服务容器构建 ─────────────────────────────

    def _build_services(self) -> Services:
        """构建服务容器，一次性注入所有依赖。"""
        cfg_mgr = ConfigManager(DATA_DIR, self._wire)
        storage = StorageManager(DATA_DIR, self._wire)
        storage.restore_state()

        plan_mgr = PlanManager(self._config, storage, self._wire)
        plan_mgr.sync_plan_mapping()

        user_mgr = UserManager(storage, plan_mgr, self._wire)

        return Services(
            api_getter=self._get_api,
            config_fn=self._config,
            config_manager=cfg_mgr,
            storage=storage,
            plan_manager=plan_mgr,
            user_manager=user_mgr,
            wire=self._wire,
        )

    # ── 日志 ──────────────────────────────────────

    def _init_plugin_logger(self) -> logging.Logger:
        os.makedirs(DATA_DIR, exist_ok=True)
        try:
            fh = RotatingFileHandler(
                PLUGIN_LOG_PATH, maxBytes=2 * 1024 * 1024, backupCount=5, encoding="utf-8"
            )
            fh.setLevel(logging.DEBUG)
            fh.setFormatter(logging.Formatter(
                "[%(asctime)s] [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
            ))
            plog = logging.getLogger("afdian_model")
            plog.addHandler(fh)
            plog.setLevel(logging.DEBUG)
            return plog
        except OSError:
            return logger  # type: ignore[return-value]

    def _wire(self, msg: str, level: str = "info") -> None:
        """双通道日志：AstrBot logger + 插件文件日志。"""
        try:
            getattr(logger, level)(f"{PLUGIN_LOG_PREFIX} {msg}")
        except AttributeError:
            logger.info(f"{PLUGIN_LOG_PREFIX} {msg}")
        try:
            getattr(self._plog, level)(msg)
        except AttributeError:
            self._plog.info(msg)

    # ── 配置 ──────────────────────────────────────

    def _config(self) -> dict:
        """获取合并配置：WebUI 优先，plugin_config.json 兜底。"""
        try:
            star_cfg = self._star_config if isinstance(self._star_config, dict) else {}
            file_cfg = self._svc.config_manager.load_plugin_config()

            if not star_cfg and not file_cfg:
                migrated = self._try_migrate_astrbot_config()
                if migrated:
                    self._svc.config_manager.save_plugin_config(migrated)
                    return migrated

            merged = dict(file_cfg) if file_cfg else {}
            star_keys = [k for k, v in star_cfg.items() if v or k not in merged]
            for k in star_keys:
                merged[k] = star_cfg[k]

            if star_keys:
                log_msg(self._wire, f"配置合并: WebUI覆盖 {star_keys}")
            if merged:
                self._svc.config_manager.save_plugin_config(merged)
            return merged
        except Exception as e:
            log_msg(self._wire, f"配置读取异常: {e}", "error")
            return {}

    def _try_migrate_astrbot_config(self) -> dict:
        try:
            plugin_dir = PLUGIN_DIR
            astrbot_data_dir = os.path.dirname(os.path.dirname(plugin_dir))
            path = os.path.join(astrbot_data_dir, "config", f"{PLUGIN_NAME}_config.json")
            if not os.path.exists(path):
                return {}
            import json
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
                if content.startswith("\ufeff"):
                    content = content[1:]
                cfg = json.loads(content)
                if cfg:
                    log_msg(self._wire, f"旧配置迁移成功: {list(cfg.keys())}")
                    return cfg
        except (json.JSONDecodeError, OSError) as e:
            log_msg(self._wire, f"迁移 AstrBot 配置失败: {e}", "warning")
        return {}

    # ── API ───────────────────────────────────────

    def _get_api(self) -> AfdianAPI | None:
        cfg = self._config()
        uid = cfg.get("afdian_user_id", "")
        token = cfg.get("afdian_token", "")
        api_base = cfg.get("afdian_api_base", "https://afdian.net")

        if not uid or not token:
            log_msg(self._wire, "API未配置", "warning")
            return None
        if self._api is None:
            try:
                self._api = AfdianAPI(uid, token, api_base, self._wire)
            except ValueError as e:
                log_msg(self._wire, f"API初始化失败: {e}", "error")
                return None
        return self._api

    # ── 权限 ──────────────────────────────────────

    async def _check_admin(self, event: AstrMessageEvent) -> bool:
        try:
            config = getattr(self.context, "astrbot_config", {}) or getattr(self.context, "_config", {})
            return str(event.get_sender_id()) in config.get("admins_id", [])
        except Exception:
            return False

    # ── 命令注册 ──────────────────────────────────

    @filter.command("afdian_help")
    async def cmd_help(self, event: AstrMessageEvent):
        """显示爱发电赞助插件使用帮助"""
        from .commands_user import cmd_help
        async for res in cmd_help(self._svc, event):
            yield res

    @filter.command("afdian_bind")
    async def cmd_bind(self, event: AstrMessageEvent):
        """绑定爱发电订单号，获得模型使用权限（仅私聊）"""
        from .commands_user import cmd_bind
        async for res in cmd_bind(self._svc, event):
            yield res

    @filter.command("afdian_models")
    async def cmd_models(self, event: AstrMessageEvent):
        """查看当前可用的模型列表"""
        from .commands_user import cmd_models
        async for res in cmd_models(self._svc, event):
            yield res

    @filter.command("afdian_switch")
    async def cmd_switch(self, event: AstrMessageEvent):
        """切换当前使用的模型（私聊为个人切换，群聊仅群主/群管可切换）"""
        from .commands_user import cmd_switch
        async for res in cmd_switch(self._svc, event, self._check_admin):
            yield res

    @filter.command("afdian_status")
    async def cmd_status(self, event: AstrMessageEvent):
        """查看赞助权限状态（剩余天数、到期时间等）"""
        from .commands_user import cmd_status
        async for res in cmd_status(self._svc, event):
            yield res

    @filter.command("afdian_reset")
    async def cmd_reset(self, event: AstrMessageEvent):
        """释放指定订单的绑定状态（管理员，仅私聊）"""
        from .commands_admin import cmd_reset
        async for res in cmd_reset(self._svc, event, self._check_admin):
            yield res

    @filter.command("afdian_reset_all")
    async def cmd_reset_all(self, event: AstrMessageEvent):
        """一键清除所有缓存和持久化数据（管理员）"""
        from .commands_admin import cmd_reset_all
        async for res in cmd_reset_all(self._svc, event, self._check_admin):
            yield res

    @filter.command("afdian_addmodels")
    async def cmd_addmodels(self, event: AstrMessageEvent):
        """批量向方案添加模型（管理员）"""
        from .commands_model import cmd_addmodels
        async for res in cmd_addmodels(self._svc, event, self._check_admin):
            yield res

    @filter.command("afdian_delmodels")
    async def cmd_delmodels(self, event: AstrMessageEvent):
        """批量移除模型或可达性测试（管理员）"""
        from .commands_model import cmd_delmodels
        async for res in cmd_delmodels(self._svc, event, self._check_admin):
            yield res

    @filter.command("afdian_addplan")
    async def cmd_addplan(self, event: AstrMessageEvent):
        """添加赞助方案（管理员）"""
        from .commands_plan import cmd_addplan
        async for res in cmd_addplan(self._svc, event, self._check_admin):
            yield res

    @filter.command("afdian_delplan")
    async def cmd_delplan(self, event: AstrMessageEvent):
        """删除赞助方案（管理员）"""
        from .commands_plan import cmd_delplan
        async for res in cmd_delplan(self._svc, event, self._check_admin):
            yield res

    @filter.command("afdian_query")
    async def cmd_query(self, event: AstrMessageEvent):
        """查询指定爱发电订单详情（管理员）"""
        from .commands_admin import cmd_query
        async for res in cmd_query(self._svc, event, self._check_admin):
            yield res

    @filter.command("afdian_getconfig")
    async def cmd_getconfig(self, event: AstrMessageEvent):
        """查看当前插件配置（管理员）"""
        from .commands_admin import cmd_getconfig
        async for res in cmd_getconfig(self._svc, event, self._check_admin):
            yield res

    @filter.command("afdian_setconfig")
    async def cmd_setconfig(self, event: AstrMessageEvent):
        """设置插件配置项（管理员）"""
        from .commands_admin import cmd_setconfig
        async for res in cmd_setconfig(self._svc, event, self._check_admin):
            yield res

    @filter.command("afdian_migrateconfig")
    async def cmd_migrateconfig(self, event: AstrMessageEvent):
        """从 AstrBot 旧配置迁移到插件配置（管理员）"""
        from .commands_admin import cmd_migrateconfig
        async for res in cmd_migrateconfig(self._svc, event, self._check_admin):
            yield res
