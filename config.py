import sys
from pathlib import Path

from loguru import logger
from sycm import ConfigManager

BASE_DIR = Path(__file__).parent
CONFIG_PATH = Path("config.yml")
DEFAULTS_PATH = BASE_DIR / "defaults" / "config.yml"

config = ConfigManager(CONFIG_PATH, defaults=DEFAULTS_PATH)

logger = logger.bind(log_type="system")

# Logging configuration
logger.remove()
logger.add(
    sys.stderr,
    level=config.logging.level,
    format="<dim>{time:YYYY-MM-DD HH:mm:ss}</dim> | <level>{level: <8}</level> | {message}",
    colorize=True,
    filter=lambda record: record["extra"].get("log_type") != "http",
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
    filter=lambda record: record["extra"].get("log_type") != "http",
)

access_log_path = Path(config.logging.access_file)
access_log_path.parent.mkdir(parents=True, exist_ok=True)
logger.add(
    str(access_log_path),
    level="INFO",
    rotation="1 day",
    retention="30 days",
    enqueue=True,
    format="{time:YYYY-MM-DD HH:mm:ss} | {message}",
    filter=lambda record: record["extra"].get("log_type") == "http",
)

access_logger = logger.bind(log_type="http")
