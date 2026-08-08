import logging
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

from qqbot.interfaces.qq.client import run_bot


def configure_logging(root: Path) -> None:
    log_dir = root / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    file_handler = TimedRotatingFileHandler(
        log_dir / "qqbot.log",
        when="midnight",
        backupCount=14,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logging.basicConfig(
        level=logging.INFO,
        handlers=[file_handler, console_handler],
        force=True,
    )


if __name__ == "__main__":
    project_root = Path(__file__).resolve().parent
    configure_logging(project_root)
    run_bot(project_root)
