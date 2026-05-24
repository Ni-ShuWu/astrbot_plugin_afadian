"""AstrBot 爱发电模型订阅插件入口。持久化完全基于官方 sp 接口。"""

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
from .utils import PLUGIN_LOG_PREFIX

PLUGIN_NAME = "astrbot_plugin_afdian_model"
PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(get_astrbot_data_path(), "plugin_data", PLUGIN_NAME)
PLUGIN_LOG_PATH = os.path.join(LOG_DIR, "plugin.log")


class AfdianModelPlugin(Star):
    """爱发电赞助模型订阅插件。"""

    def __init__(self, context: Context, config: AstrBotConfig = None) -> None:
        super().__init__(context)
        self._star_config = config if config else {}
        self._api: AfdianAPI | None = None
        self._plog = self._init_plugin_logger()
        self._svc = self._build_services()
        self._cron = CronTasks(self._svc)
        asyncio.create_task(self._cron.cron_daily())
        asyncio.create_task(self._cron.cron_poll())

    def _build_services(self) -> Services:
        cfg_mgr = ConfigManager(self._wire)
        storage = StorageManager(self._wire)
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

    def _init_plugin_logger(self) -> logging.Logger:
        os.makedirs(LOG_DIR, exist_ok=True)
        try:
            fh = RotatingFileHandler(PLUGIN_LOG_PATH, maxBytes=2*1024*1024, backupCount=5, encoding="utf-8")
            fh.setLevel(logging.DEBUG)
            fh.setFormatter(logging.Formatter("[%(asctime)s] [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
            plog = logging.getLogger("afdian_model")
            plog.addHandler(fh)
            plog.setLevel(logging.DEBUG)
            return plog
        except OSError:
            return logger  # type: ignore[return-value]

    def _wire(self, msg: str, level: str = "info") -> None:
        text = f"{PLUGIN_LOG_PREFIX} {msg}"
        getattr(logger, level, logger.info)(text)
        getattr(self._plog, level, self._plog.info)(msg)

    def _config(self) -> dict:
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
            if merged:
                self._svc.config_manager.save_plugin_config(merged)
            return merged
        except Exception as e:
            self._wire(f"配置读取异常: {e}", "error")
            return {}

    def _try_migrate_astrbot_config(self) -> dict:
        try:
            astrbot_data_dir = os.path.dirname(os.path.dirname(PLUGIN_DIR))
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
                    self._wire(f"旧配置迁移成功: {list(cfg.keys())}")
                    return cfg
        except (json.JSONDecodeError, OSError) as e:
            self._wire(f"迁移 AstrBot 配置失败: {e}", "warning")
        return {}

    def _get_api(self) -> AfdianAPI | None:
        cfg = self._config()
        uid = cfg.get("afdian_user_id", "")
        token = cfg.get("afdian_token", "")
        if not uid or not token:
            self._wire("API未配置", "warning")
            return None
        if self._api is None:
            try:
                self._api = AfdianAPI(uid, token, cfg.get("afdian_api_base", "https://afdian.net"), self._wire)
            except ValueError as e:
                self._wire(f"API初始化失败: {e}", "error")
                return None
        return self._api

    async def _check_admin(self, event: AstrMessageEvent) -> bool:
        try:
            config = getattr(self.context, "astrbot_config", {}) or getattr(self.context, "_config", {})
            return str(event.get_sender_id()) in config.get("admins_id", [])
        except Exception:
            return False

    # ── 命令注册（懒 import 避免循环依赖）─────────
    @filter.command("afdian_help")
    async def cmd_help(self, event: AstrMessageEvent):
        from .commands_user import cmd_help
        async for res in cmd_help(self._svc, event): yield res

    @filter.command("afdian_bind")
    async def cmd_bind(self, event: AstrMessageEvent):
        from .commands_user import cmd_bind
        async for res in cmd_bind(self._svc, event): yield res

    @filter.command("afdian_models")
    async def cmd_models(self, event: AstrMessageEvent):
        from .commands_user import cmd_models
        async for res in cmd_models(self._svc, event): yield res

    @filter.command("afdian_switch")
    async def cmd_switch(self, event: AstrMessageEvent):
        from .commands_user import cmd_switch
        async for res in cmd_switch(self._svc, event, self._check_admin): yield res

    @filter.command("afdian_status")
    async def cmd_status(self, event: AstrMessageEvent):
        from .commands_user import cmd_status
        async for res in cmd_status(self._svc, event): yield res

    @filter.command("afdian_reset")
    async def cmd_reset(self, event: AstrMessageEvent):
        from .commands_admin import cmd_reset
        async for res in cmd_reset(self._svc, event, self._check_admin): yield res

    @filter.command("afdian_reset_all")
    async def cmd_reset_all(self, event: AstrMessageEvent):
        from .commands_admin import cmd_reset_all
        async for res in cmd_reset_all(self._svc, event, self._check_admin): yield res

    @filter.command("afdian_addmodels")
    async def cmd_addmodels(self, event: AstrMessageEvent):
        from .commands_model import cmd_addmodels
        async for res in cmd_addmodels(self._svc, event, self._check_admin): yield res

    @filter.command("afdian_delmodels")
    async def cmd_delmodels(self, event: AstrMessageEvent):
        from .commands_model import cmd_delmodels
        async for res in cmd_delmodels(self._svc, event, self._check_admin): yield res

    @filter.command("afdian_addplan")
    async def cmd_addplan(self, event: AstrMessageEvent):
        from .commands_plan import cmd_addplan
        async for res in cmd_addplan(self._svc, event, self._check_admin): yield res

    @filter.command("afdian_delplan")
    async def cmd_delplan(self, event: AstrMessageEvent):
        from .commands_plan import cmd_delplan
        async for res in cmd_delplan(self._svc, event, self._check_admin): yield res

    @filter.command("afdian_query")
    async def cmd_query(self, event: AstrMessageEvent):
        from .commands_admin import cmd_query
        async for res in cmd_query(self._svc, event, self._check_admin): yield res

    @filter.command("afdian_getconfig")
    async def cmd_getconfig(self, event: AstrMessageEvent):
        from .commands_admin import cmd_getconfig
        async for res in cmd_getconfig(self._svc, event, self._check_admin): yield res

    @filter.command("afdian_setconfig")
    async def cmd_setconfig(self, event: AstrMessageEvent):
        from .commands_admin import cmd_setconfig
        async for res in cmd_setconfig(self._svc, event, self._check_admin): yield res

    @filter.command("afdian_migrateconfig")
    async def cmd_migrateconfig(self, event: AstrMessageEvent):
        from .commands_admin import cmd_migrateconfig
        async for res in cmd_migrateconfig(self._svc, event, self._check_admin): yield res
