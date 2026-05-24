"""插件配置管理 —— 基于 AstrBot sp 接口，不再维护 plugin_config.json。"""

from astrbot.core import sp

from .utils import log_msg

SP_PLUGIN_CONFIG = "afdian_model:plugin_config"


class ConfigManager:
    """插件配置管理器。"""

    def __init__(self, wire_fn=None) -> None:
        self._wire = wire_fn

    def load_plugin_config(self) -> dict:
        cfg = sp.get(SP_PLUGIN_CONFIG, {})
        if cfg and isinstance(cfg, dict):
            log_msg(self._wire, f"插件配置加载成功: {list(cfg.keys())}")
            return cfg
        return {}

    def save_plugin_config(self, cfg: dict) -> None:
        sp.put(SP_PLUGIN_CONFIG, cfg)
        log_msg(self._wire, f"插件配置保存成功: {list(cfg.keys())}")
