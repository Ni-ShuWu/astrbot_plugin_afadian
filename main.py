import asyncio
import logging
import os
from logging.handlers import RotatingFileHandler

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core import sp

from .afdian_api import AfdianAPI
from .config import ConfigManager
from .storage import StorageManager
from .plan_manager import PlanManager
from .user_manager import UserManager
from .commands_user import UserCommands
from .commands_admin import AdminCommands
from .cron_tasks import CronTasks


PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(PLUGIN_DIR, "data")
PLUGIN_LOG_PATH = os.path.join(DATA_DIR, "plugin.log")


class AfdianModelPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self._api = None
        self._context = context
        self._star_config = config if config else {}

        self._init_data_dir()

        self._config_manager = ConfigManager(self._wire)
        self._storage = StorageManager(self._wire)
        self._plan_manager = PlanManager(self._config, self._wire)
        self._user_manager = UserManager(self._storage, self._plan_manager, self._wire)

        self._user_commands = UserCommands(
            self._get_api, self._config, self._config_manager, 
            self._storage, self._plan_manager, self._user_manager, self._wire
        )
        self._admin_commands = AdminCommands(
            self._get_api, self._config, self._config_manager, 
            self._storage, self._plan_manager, self._wire
        )
        self._cron_tasks = CronTasks(
            self._get_api, self._storage, self._plan_manager, self._wire
        )

        self._plan_manager.sync_plan_mapping()

        asyncio.create_task(self._cron_tasks._cron_daily())
        asyncio.create_task(self._cron_tasks._cron_poll())

    def _init_data_dir(self):
        os.makedirs(DATA_DIR, exist_ok=True)
        try:
            fh = RotatingFileHandler(
                PLUGIN_LOG_PATH, maxBytes=2 * 1024 * 1024, backupCount=5, encoding="utf-8"
            )
            fh.setLevel(logging.DEBUG)
            fh.setFormatter(logging.Formatter(
                "[%(asctime)s] [%(levelname)s] %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S"
            ))
            plog = logging.getLogger("afdian_model")
            plog.addHandler(fh)
            plog.setLevel(logging.DEBUG)
            self._plog = plog
        except Exception:
            self._plog = logger

    def _wire(self, msg: str, level: str = "info"):
        getattr(logger, level)(msg)
        getattr(self._plog, level)(msg)

    def _config(self) -> dict:
        try:
            plugin_cfg = self._config_manager.load_plugin_config()
            if plugin_cfg:
                return plugin_cfg

            try:
                plugin_dir = os.path.dirname(DATA_DIR)
                astrbot_data_dir = os.path.dirname(os.path.dirname(plugin_dir))

                astrbot_cfg_path = os.path.join(astrbot_data_dir, "config", "astrbot_plugin_afdian_model_config.json")
                if os.path.exists(astrbot_cfg_path):
                    import json
                    self._wire(f"[AfdianModel] 尝试从 AstrBot 配置迁移: {astrbot_cfg_path}", "info")
                    with open(astrbot_cfg_path, "r", encoding="utf-8") as f:
                        content = f.read()
                        if content.startswith('\ufeff'):
                            content = content[1:]
                        astrbot_cfg = json.loads(content)
                        if astrbot_cfg:
                            self._config_manager.save_plugin_config(astrbot_cfg)
                            return astrbot_cfg
            except Exception as e:
                self._wire(f"[AfdianModel] 迁移 AstrBot 配置失败: {e}", "warning")

            cfg = self._star_config if isinstance(self._star_config, dict) and self._star_config else {}
            if cfg:
                self._config_manager.save_plugin_config(cfg)
            self._wire(f"[AfdianModel] 使用初始化配置: keys={list(cfg.keys()) if cfg else 'EMPTY'}", "info")
            return cfg
        except Exception as e:
            self._wire(f"[AfdianModel] 配置读取异常: {e}", "error")
            return {}

    def _get_api(self):
        cfg = self._config()
        uid = cfg.get("afdian_user_id", "")
        token = cfg.get("afdian_token", "")
        api_base = cfg.get("afdian_api_base", "https://afdian.net")
        self._wire(
            f"[AfdianModel] API配置检查: user_id={'***' if uid else 'EMPTY'} "
            f"token={'***' if token else 'EMPTY'} base={api_base}",
            "info"
        )
        if not uid or not token:
            self._wire(
                f"[AfdianModel] API未配置: user_id={'***' if uid else 'EMPTY'} "
                f"token={'***' if token else 'EMPTY'}",
                "warning"
            )
            return None
        try:
            self._api = AfdianAPI(uid, token, api_base, self._wire)
            self._wire("[AfdianModel] API初始化成功")
        except Exception as e:
            self._wire(f"[AfdianModel] API初始化失败: {e}", "error")
            return None
        return self._api

    async def _check_admin(self, event: AstrMessageEvent) -> bool:
        try:
            config = getattr(self.context, "astrbot_config", {})
            if not config:
                config = getattr(self.context, "_config", {})
            admins = config.get("admins_id", [])
            return str(event.get_sender_id()) in admins
        except Exception:
            return False

    @filter.command("afdian_help")
    async def cmd_help(self, event: AstrMessageEvent):
        async for res in self._user_commands.cmd_help(event):
            yield res

    @filter.command("afdian_bind")
    async def cmd_bind(self, event: AstrMessageEvent):
        async for res in self._user_commands.cmd_bind(event):
            yield res

    @filter.command("afdian_models")
    async def cmd_models(self, event: AstrMessageEvent):
        async for res in self._user_commands.cmd_models(event):
            yield res

    @filter.command("afdian_switch")
    async def cmd_switch(self, event: AstrMessageEvent):
        async for res in self._user_commands.cmd_switch(event, self._check_admin):
            yield res

    @filter.command("afdian_status")
    async def cmd_status(self, event: AstrMessageEvent):
        async for res in self._user_commands.cmd_status(event):
            yield res

    @filter.command("afdian_reset")
    async def cmd_reset(self, event: AstrMessageEvent):
        async for res in self._admin_commands.cmd_reset(event, self._check_admin):
            yield res

    @filter.command("afdian_reset_all")
    async def cmd_reset_all(self, event: AstrMessageEvent):
        async for res in self._admin_commands.cmd_reset_all(event, self._check_admin):
            yield res

    @filter.command("afdian_addmodels")
    async def cmd_addmodels(self, event: AstrMessageEvent):
        async for res in self._admin_commands.cmd_addmodels(event, self._check_admin):
            yield res

    @filter.command("afdian_delmodels")
    async def cmd_delmodels(self, event: AstrMessageEvent):
        async for res in self._admin_commands.cmd_delmodels(event, self._check_admin):
            yield res

    @filter.command("afdian_addplan")
    async def cmd_addplan(self, event: AstrMessageEvent):
        async for res in self._admin_commands.cmd_addplan(event, self._check_admin):
            yield res

    @filter.command("afdian_delplan")
    async def cmd_delplan(self, event: AstrMessageEvent):
        async for res in self._admin_commands.cmd_delplan(event, self._check_admin):
            yield res

    @filter.command("afdian_query")
    async def cmd_query(self, event: AstrMessageEvent):
        async for res in self._admin_commands.cmd_query(event, self._check_admin):
            yield res

    @filter.command("afdian_getconfig")
    async def cmd_getconfig(self, event: AstrMessageEvent):
        async for res in self._admin_commands.cmd_getconfig(event, self._check_admin):
            yield res

    @filter.command("afdian_setconfig")
    async def cmd_setconfig(self, event: AstrMessageEvent):
        async for res in self._admin_commands.cmd_setconfig(event, self._check_admin):
            yield res

    @filter.command("afdian_migrateconfig")
    async def cmd_migrateconfig(self, event: AstrMessageEvent):
        async for res in self._admin_commands.cmd_migrateconfig(event, self._check_admin):
            yield res
