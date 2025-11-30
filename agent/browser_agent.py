import json
import logging
from typing import Dict, List, Optional

from anthropic import Anthropic

from agent.dom_sub_agent import DomSubAgent
from core.config import AgentConfig
from core.prompts import SYSTEM_PROMPT, TOOLS
from infrastructure.tools import ToolExecutor


class BrowserAgent:
    def __init__(self, config: AgentConfig, executor: ToolExecutor) -> None:
        self.config = config
        self.executor = executor
        self.client = Anthropic()
        self.dom_agent = DomSubAgent(self.client, self.config.model)
        self.executor.dom_agent = self.dom_agent
        self.summary: str = ""
        self._printed_header = False
        self._finished = False

    def _serialize_blocks(self, blocks) -> List[Dict]:
        serialized: List[Dict] = []
        for block in blocks:
            if block.type == "text":
                serialized.append({"type": "text", "text": block.text})
            elif block.type == "tool_use":
                serialized.append(
                    {
                        "type": "tool_use",
                        "id": block.id,
                        "name": block.name,
                        "input": block.input,
                    }
                )
        return serialized
    
    def _format_params(self, params: Dict, max_len: int = 160) -> str:
        """Сделать компактное описание параметров tool'а для лога."""
        parts = []
        for k, v in params.items():
            vs = repr(v)
            if len(vs) > 40:
                vs = vs[:37] + "…"
            parts.append(f"{k}={vs}")
        line = ", ".join(parts)
        if len(line) > max_len:
            line = line[: max_len - 1] + "…"
        return line
    
    def _should_auto_finish_from_text(self, text: str) -> bool:
        """
        Эвристика авто-остановки:
        считаем задачу завершённой только если модель сама явно пишет,
        что задача выполнена / цель достигнута / как вы просили, оплату не производил.
        На любые промежуточные рассуждения ("может указывать на то, что...") не реагируем.
        """
        if not text:
            return False

        t = text.lower()

        finish_markers = [
            "задача выполнена",
            "задача полностью выполнена",
            "задача целиком выполнена",
            "задача решена",
            "цель достигнута",
            "цель задачи достигнута",
        ]

        return any(m in t for m in finish_markers)

    
    def _is_potentially_destructive(self, name: str, params: Dict) -> bool:
        """
        Эвристически решаем, может ли действие быть деструктивным:
        оплата, удаление, отправка и т.п.
        Подтверждение запрашиваем ТОЛЬКО для таких действий.
        """
        # Если флаг --confirm-actions не включён — вообще ничего не спрашиваем
        if not self.config.confirm_actions:
            return False

        # Нас интересуют только клики по элементам/тексту
        if name not in {"click_element", "click_text"}:
            return False

        dangerous_keywords = [
            # Оплата/покупка
            "оплатить", "оплата", "заплатить",
            "pay", "payment", "checkout", "purchase",
            "buy now", "buy", "order", "place order",
            # Удаление/очистка
            "удалить", "удаление", "delete", "remove",
            "trash", "очистить", "clear all", "очистка",
            # Отправка/публикация
            "отправить", "submit", "send",
            "публиковать", "publish", "post",
            # Отписки и т.п.
            "unsubscribe", "отписаться",
            "архивировать", "archive",
        ]

        label = ""

        if name == "click_text":
            label = str(params.get("text") or "").lower()

        elif name == "click_element":
            # Пытаемся вытащить подпись элемента из последней дистилляции
            try:
                target_id = int(params.get("element_id"))
            except (TypeError, ValueError):
                target_id = None

            if target_id is not None:
                for el in self.executor.last_elements:
                    if el.id == target_id:
                        label = " ".join(
                            [
                                (el.text or ""),
                                (el.aria_label or ""),
                                (el.placeholder or ""),
                                (el.href or ""),
                            ]
                        ).lower()
                        break

        if not label:
            return False

        return any(kw in label for kw in dangerous_keywords)


    def _print_tool_call(self, name: str, params: Dict) -> None:
        """Одна строка в стиле: 🛠 Using tool: navigate_url(url='https://...')."""
        pretty = self._format_params(params)
        self._log(f"\n🛠 Using tool: {name}({pretty})")
        logging.debug("TOOL_CALL %s %s", name, json.dumps(params, ensure_ascii=False))

    
    def _print_assistant_text(self, text: str) -> None:
        """Коротко логируем мысли/ответ ассистента."""
        shown = (text or "").strip()
        max_len = 800
        if len(shown) > max_len:
            shown = shown[:max_len] + "...<truncated>"
        self._log(f"\nAssistant: {shown}")


    def _summarize_history(self, history: List[Dict], prior_summary: str) -> str:
        """Summarize old turns to keep context small while preserving user constraints."""
        try:
            summary_prompt = (
                "Сделай краткое резюме предыдущих шагов агента (до 120 слов). "
                "ОБЯЗАТЕЛЬНО сохрани все ограничения и запреты пользователя "
                "(например: 'оплату не производи', 'ничего не удаляй', 'не оформляй заказ'). "
                "Кратко перечисли ключевые действия, состояние корзины/логина и ВСЕ важные запреты. "
                "Пиши на русском языке."
            )
            response = self.client.messages.create(
                model=self.config.model,
                max_tokens=200,
                temperature=0,
                system=summary_prompt,
                messages=[
                    {
                        "role": "assistant",
                        "content": f"Предыдущая сводка:\n{prior_summary}",
                    },
                    {
                        "role": "user",
                        "content": f"История:\n{json.dumps(history, ensure_ascii=False)}",
                    },
                ],
            )
            parts = [blk.text for blk in response.content if blk.type == "text"]
            new_summary = "\n".join(parts).strip()
            return new_summary or prior_summary
        except Exception:
            return prior_summary


    def _apply_history_window(self, messages: List[Dict]) -> List[Dict]:
        """Keep last N turns verbatim; summarize older ones while retaining the original task."""
        window = max(3, self.config.history_window)

        if len(messages) <= window:
            return messages

        first = messages[0]
        rest = messages[1:]

        if len(rest) <= window:
            return messages

        older = rest[: len(rest) - window]
        recent = rest[len(rest) - window :]

        self.summary = self._summarize_history(older, self.summary)
        summary_block = {
            "role": "assistant",
            "content": [
                {
                    "type": "text",
                    "text": f"[Сводка]\n{self.summary}",
                }
            ],
        }

        pruned = [first, summary_block] + recent

        cleaned: List[Dict] = []
        prev: Optional[Dict] = None

        for msg in pruned:
            content = msg.get("content")
            is_tool_result_msg = False

            if isinstance(content, list):
                for blk in content:
                    if isinstance(blk, dict) and blk.get("type") == "tool_result":
                        is_tool_result_msg = True
                        break

            if is_tool_result_msg:
                if not prev or prev.get("role") != "assistant":
                    continue

                prev_content = prev.get("content")
                has_tool_use = False
                if isinstance(prev_content, list):
                    for blk in prev_content:
                        if isinstance(blk, dict) and blk.get("type") == "tool_use":
                            has_tool_use = True
                            break
                if not has_tool_use:
                    continue

            cleaned.append(msg)
            prev = msg

        return cleaned


    def _print_user_header(self, task: str) -> None:
        if self._printed_header:
            return
        self._log(f"You: {task}")
        self._printed_header = True

    def _print_tool_result(self, result: Dict) -> None:
        success = result.get("success", False)
        action = result.get("action") or "tool"
        message = result.get("message") or ""
        error_type = result.get("error_type")
        data = result.get("data") or {}

        status_icon = "✅" if success else "⚠️"

        if success:
            line = f"{status_icon} {action}: {message}"
        else:
            et = f" [{error_type}]" if error_type else ""
            line = f"{status_icon} {action}{et}: {message}"

        self._log(line)

        extra_parts = []
        if action == "navigate_url" and isinstance(data, dict) and data.get("url"):
            extra_parts.append(f"url={data['url']}")
        if action == "take_screenshot" and isinstance(data, dict) and data.get("path"):
            extra_parts.append(f"path={data['path']}")
        if action == "wait_for_element" and isinstance(data, dict) and data.get("query"):
            extra_parts.append(f"query='{data['query']}'")
        if action == "query_dom" and isinstance(data, dict) and data.get("answer"):
            answer = str(data["answer"]).strip()
            if len(answer) > 400:
                answer = answer[:397] + "…"
            extra_parts.append(f"DOM answer: {answer}")

        if extra_parts:
            self._log("   " + " | ".join(extra_parts))

        logging.debug("TOOL_RESULT %s", json.dumps(result, ensure_ascii=False))


    def _truncate_content(self, content: str, max_len: int = 4000) -> str:
        if len(content) <= max_len:
            return content
        return content[:max_len] + "...<truncated>"

    def _log(self, msg: str) -> None:
        print(msg)
        logging.info(msg)

    def run(self) -> None:
        messages = [{"role": "user", "content": self.config.task}]
        self._print_user_header(self.config.task)

        for iteration in range(1, self.config.max_iterations + 1):
            self._log(f"\n==== Итерация {iteration}/{self.config.max_iterations} ====")
            response = self.client.messages.create(
                model=self.config.model,
                max_tokens=1200,
                temperature=self.config.temperature,
                system=SYSTEM_PROMPT,
                messages=messages,
                tools=TOOLS,
            )
            assistant_content = self._serialize_blocks(response.content)
            messages.append({"role": "assistant", "content": assistant_content})

            tool_results = []
            for block in response.content:
                if block.type == "text":
                    self._print_assistant_text(block.text)

                    # авто-остановка ТОЛЬКО на финальном тексте
                    if self._should_auto_finish_from_text(block.text):
                        summary = self._truncate_content(block.text.strip(), max_len=800)
                        self.summary = summary
                        self._log("\n=== ЗАДАЧА ЗАВЕРШЕНА (по тексту агента) ===")
                        self._log(summary)
                        return

                if block.type == "tool_use":
                    self._print_tool_call(block.name, block.input)
                    result = self._dispatch_tool(block.name, block.input)
                    if block.name == "finish_task":
                        self._log("Агент завершил задачу через finish_task.")
                        self._print_tool_result(result)
                        return

                    payload = result
                    if (
                        block.name == "take_screenshot"
                        and isinstance(result.get("data"), dict)
                        and result["data"].get("base64_png")
                    ):
                        meta_result = {k: v for k, v in result.items()}
                        meta_data = dict(meta_result.get("data") or {})
                        meta_data.pop("base64_png", None)
                        meta_result["data"] = meta_data
                        payload = meta_result

                    content_str = json.dumps(payload, ensure_ascii=False)
                    content_str = self._truncate_content(content_str, max_len=4000)
                    tool_results.append(
                        {"type": "tool_result", "tool_use_id": block.id, "content": content_str}
                    )
                    self._print_tool_result(result)

            if tool_results:
                messages.append({"role": "user", "content": tool_results})
                messages = self._apply_history_window(messages)
                continue

            if response.stop_reason == "end_turn":
                self._log("Агент завершил разговор.")
                break


            messages = self._apply_history_window(messages)

        else:
            self._log("Max iterations reached without explicit completion.")

    def _dispatch_tool(self, name: str, params: Dict) -> Dict:
        if self._is_potentially_destructive(name, params):
            print(
                f"⚠️  Потенциально рискованное действие: {name} с параметрами {params}. "
                f"Выполнить? (y/N): ",
                end="",
                flush=True,
            )
            ans = input().strip().lower()
            if ans not in {"y", "yes", "д", "да"}:
                return {
                    "success": False,
                    "action": name,
                    "message": "Action cancelled by user",
                    "error_type": "UserCancelled",
                    "data": {"params": params},
                }

        if name == "analyze_page":
            return self.executor.analyze_page(params.get("response_format", "concise"))
        if name == "click_element":
            return self.executor.click_element(int(params["element_id"]))
        if name == "type_text":
            return self.executor.type_text(
                int(params["element_id"]), params["text"], params.get("press_enter", False)
            )
        if name == "click_and_type":
            return self.executor.click_and_type(
                int(params["element_id"]), params["text"], params.get("press_enter", True)
            )
        if name == "click_text":
            return self.executor.click_text(params["text"], bool(params.get("exact", False)))
        if name == "navigate_url":
            return self.executor.navigate_url(params["url"])
        if name == "take_screenshot":
            return self.executor.take_screenshot(params.get("label"), params.get("embed_b64", False))
        if name == "wait_for_element":
            return self.executor.wait_for_element(params["query"], float(params.get("timeout", 5)))
        if name == "search_elements":
            return self.executor.search_elements(params["query"], int(params.get("max_results", 5)))
        if name == "validate_task_complete":
            return self.executor.validate_task_complete(params.get("hint"))
        if name == "query_dom":
            return self.executor.query_dom(params["query"])
        if name == "finish_task":
            summary = params.get("summary", "")
            self._finished = True
            self._log("\n=== ЗАДАЧА ЗАВЕРШЕНА ===")
            self._log(summary)
            return {
                "success": True,
                "action": "finish_task",
                "message": "Task finished",
                "data": {"summary": summary},
            }
        if name == "scroll_page":
            return self.executor.scroll_page(params.get("direction", "down"), int(params.get("amount", 800)))
        if name == "switch_to_page":
            return self.executor.switch_to_page(int(params.get("index", -1)))
        if name == "go_back":
            return self.executor.go_back()
        if name == "extract_text":
            return self.executor.extract_text(
                params["selector"],
                bool(params.get("all_matches", False)),
                int(params.get("max_chars", 4000)),
            )
        if name == "collect_elements":
            return self.executor.collect_elements(params["selector"], int(params.get("limit", 20)))
        if name == "switch_frame":
            selector = params.get("selector")
            idx = int(params.get("index", 0))
            return self.executor.switch_frame(selector, idx)
        if name == "close_modal":
            return self.executor.close_modal()
        return {"error": f"Unknown tool '{name}'"}
