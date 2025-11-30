from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional


@dataclass
class AgentConfig:
    model: str
    task: str
    session_path: Path
    headless: bool
    max_iterations: int
    screenshot_dir: Optional[Path]
    manual_login: bool = False
    confirm_actions: bool = False
    history_window: int = 7
    temperature: float = 0


@dataclass
class DistilledElement:
    id: int
    agent_id: str
    tag: str
    role: Optional[str]
    input_type: Optional[str]
    text: str
    placeholder: Optional[str]
    aria_label: Optional[str]
    href: Optional[str]
    location: str


@dataclass
class DomTask:
    question: str
    snapshot: Dict


@dataclass
class ToolCall:
    name: str
    params: Dict

