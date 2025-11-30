import json
from typing import Dict

from anthropic import Anthropic


class DomSubAgent:
    """LLM-подагент, который отвечает на вопросы о текущей странице по JSON-снимку DOM."""

    def __init__(self, client: Anthropic, model: str) -> None:
        self.client = client
        self.model = model

    def answer(self, question: str, snapshot: Dict) -> str:
        """
        question — естественно-языковой вопрос (на русском) о том, что сейчас видно на странице.
        snapshot — dict из analyze_page(): title, url, elements и т.п.
        """
        system = (
            "Ты DOM-подагент. Тебе дают JSON со структурой страницы (URL, title, список элементов).\n"
            "Отвечай кратко и точно на русском языке на вопросы о том, что сейчас видно пользователю.\n"
            "Не придумывай элементы, которых нет в данных. Если чего-то не видно, так и скажи."
        )
        payload = json.dumps(snapshot, ensure_ascii=False)
        response = self.client.messages.create(
            model=self.model,
            system=system,
            max_tokens=400,
            temperature=0,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "JSON страницы:\n" + payload},
                        {"type": "text", "text": "\nВопрос:\n" + question},
                    ],
                }
            ],
        )
        parts = [blk.text for blk in response.content if blk.type == "text"]
        return "\n".join(parts).strip()
