import argparse
import os
from pathlib import Path

from agent.browser_agent import BrowserAgent
from core.config import AgentConfig, DEFAULT_MODEL
from infrastructure.browser_session import BrowserSession
from infrastructure.tools import ToolExecutor


def parse_args() -> AgentConfig:
    parser = argparse.ArgumentParser(description="Anthropic-powered browser automation agent")
    parser.add_argument("--task", required=True, help="Natural language task to execute")
    parser.add_argument("--session-path", default=".playwright-profile", help="Persistent user data dir")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Anthropic model name")
    parser.add_argument("--max-iterations", type=int, default=12, help="Max agent iterations")
    parser.add_argument("--headless", action="store_true", help="Run Playwright in headless mode")
    parser.add_argument("--screenshot-dir", default=".shots", help="Where to save screenshots")
    parser.add_argument("--temperature", type=float, default=0.0, help="Sampling temperature")
    parser.add_argument(
        "--manual-login",
        action="store_true",
        help="Open browser for manual login first; press Enter in terminal to start the agent.",
    )
    parser.add_argument(
        "--confirm-actions",
        action="store_true",
        help="Ask for confirmation in terminal before executing potentially destructive browser actions.",
    )
    parser.add_argument(
        "--history-window",
        type=int,
        default=7,
        help="How many recent turns to keep verbatim before summarizing older history.",
    )
    parser.add_argument(
        "--max-stuck-steps",
        type=int,
        default=0,
        help="Deprecated; no stuck detection. Ignored.",
    )
    args = parser.parse_args()

    return AgentConfig(
        model=args.model,
        task=args.task,
        session_path=Path(args.session_path),
        headless=args.headless,
        max_iterations=args.max_iterations,
        screenshot_dir=Path(args.screenshot_dir) if args.screenshot_dir else None,
        manual_login=args.manual_login,
        confirm_actions=args.confirm_actions,
        history_window=args.history_window,
        temperature=args.temperature,
    )


def main() -> None:
    config = parse_args()
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise SystemExit("ANTHROPIC_API_KEY is not set.")

    with BrowserSession(config.session_path, config.headless) as session:
        executor = ToolExecutor(session, config.screenshot_dir)
        agent = BrowserAgent(config, executor)
        agent.run()


if __name__ == "__main__":
    main()
