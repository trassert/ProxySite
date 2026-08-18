import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

from loguru import logger

CONFIG_PATH = Path("config.toml")
SYSTEM_LOG_TEMPLATE = "logs/system-{time:DD.MM.YY}.log"
ACCESS_LOG_TEMPLATE = "logs/access-{time:DD.MM.YY}.log"

DEFAULT_CONFIG = {
    "app": {
        "debug": False,
        "password": "",
    },
    "logging": {
        "level": "INFO",
        "file": SYSTEM_LOG_TEMPLATE,
        "rotation": "10 MB",
        "retention": "7 days",
    },
    "telegram": {
        "enabled": False,
        "api_id": 0,
        "api_hash": "",
        "session_name": "proxyhub",
        "channels": ["telemtrs"],
    },
}


@dataclass
class AppConfig:
    debug: bool = False
    password: str = ""


@dataclass
class LoggingConfig:
    level: str = "INFO"
    file: str = SYSTEM_LOG_TEMPLATE
    rotation: str = "10 MB"
    retention: str = "7 days"


@dataclass
class TelegramConfig:
    enabled: bool = False
    api_id: int = 0
    api_hash: str = ""
    session_name: str = "proxyhub"
    channels: list[str] = None


@dataclass
class Config:
    app: AppConfig
    logging: LoggingConfig
    telegram: TelegramConfig


def load_config(path: Path = CONFIG_PATH) -> Config:
    config_data = DEFAULT_CONFIG.copy()
    if path.exists():
        try:
            with path.open("rb") as fp:
                loaded = tomllib.load(fp)
            for section, values in loaded.items():
                if section not in config_data:
                    config_data[section] = values
                elif isinstance(values, dict):
                    config_data[section].update(values)
        except Exception as exc:
            logger.warning("Failed to read config.toml: {exc}", exc=exc)
    else:
        logger.warning(
            "Config file {path} not found, using defaults", path=path
        )

    telegram = TelegramConfig(
        enabled=bool(config_data["telegram"].get("enabled", False)),
        api_id=int(config_data["telegram"].get("api_id", 0)),
        api_hash=str(config_data["telegram"].get("api_hash", "")),
        session_name=str(
            config_data["telegram"].get("session_name", "proxyhub")
        ),
        channels=list(config_data["telegram"].get("channels", ["telemtrs"]))
        or [],
    )
    logging = LoggingConfig(
        level=str(config_data["logging"].get("level", "INFO")),
        file=str(config_data["logging"].get("file", SYSTEM_LOG_TEMPLATE)),
        rotation=str(config_data["logging"].get("rotation", "10 MB")),
        retention=str(config_data["logging"].get("retention", "7 days")),
    )
    app = AppConfig(
        debug=bool(config_data["app"].get("debug", False)),
        password=str(config_data["app"].get("password", "")),
    )
    return Config(app=app, logging=logging, telegram=telegram)


config = load_config()

# Logging configuration
logger.remove()
logger.add(
    sys.stderr,
    level=config.logging.level,
    format="<green>{time:MM-DD HH:mm:ss}</green> | <level>{level}</level> | <cyan>{function}</cyan> - <level>{message}</level>",
)

log_file_path = Path(config.logging.file)
log_file_path.parent.mkdir(parents=True, exist_ok=True)
logger.add(
    str(log_file_path),
    level=config.logging.level,
    rotation=config.logging.rotation,
    retention=config.logging.retention,
    enqueue=True,
    backtrace=True,
    diagnose=False,
)

access_log_path = Path(ACCESS_LOG_TEMPLATE)
access_log_path.parent.mkdir(parents=True, exist_ok=True)
logger.add(
    str(access_log_path),
    level="INFO",
    rotation="1 day",
    retention="30 days",
    enqueue=True,
    format="{time:DD.MM.YY HH:mm:ss} | {message}",
    filter=lambda record: record["extra"].get("log_type") == "http",
)

access_logger = logger.bind(log_type="http")
