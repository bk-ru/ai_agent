import logging

from core.models import AgentConfig

DEFAULT_MODEL = "claude-sonnet-4-5-20250929"

logging.basicConfig(
    filename="log.txt",
    filemode="a",
    level=logging.INFO,
    format="%(asctime)s %(message)s",
    encoding="utf-8",
)
