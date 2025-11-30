import base64
import json
import logging
import time
from pathlib import Path
from typing import Dict, List, Optional

from playwright.sync_api import Page, TimeoutError

from core.models import DistilledElement
from infrastructure.browser_session import BrowserSession


class ToolExecutor:
    """Implements the tool surface exposed to the LLM."""

    def __init__(self, session: BrowserSession, screenshot_dir: Optional[Path]) -> None:
        self.session = session
        self.screenshot_dir = screenshot_dir
        self.last_elements: List[DistilledElement] = []
        self.dom_agent = None

    def _result(
        self,
        success: bool,
        action: str,
        message: str,
        error_type: Optional[str] = None,
        error: Optional[str] = None,
        suggestion: Optional[str] = None,
        data: Optional[Dict] = None,
    ) -> Dict:
        return {
            "success": success,
            "action": action,
            "message": message,
            "error_type": error_type,
            "error": error,
            "suggestion": suggestion,
            "data": data,
        }

    @property
    def page(self) -> Page:
        if not self.session.page:
            raise RuntimeError("Browser page not initialized")
        return self.session.page

    def _sync_active_page(self) -> None:
        """If a click opened a new tab, keep using the newest page."""
        if not self.session.context:
            return
        if self.session.context.pages and self.session.context.pages[-1] is not self.session.page:
            self.session.page = self.session.context.pages[-1]

    def analyze_page(self, response_format: str = "concise") -> Dict:
        """Return distilled interactive elements with semantic IDs."""
        self._sync_active_page()
        stamp = str(int(time.time() * 1000))
        selector = "button, input, textarea, select, option, a, [role=button], [role=link], [onclick]"
        elements: List[DistilledElement] = []

        try:
            raw_elements = self.page.eval_on_selector_all(
                selector,
                """
                (nodes, stamp) => {
                  const isVisible = (el) => {
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    if (!rect || rect.width === 0 || rect.height === 0) return false;
                    if (style.visibility === 'hidden' || style.display === 'none' || Number(style.opacity) === 0) return false;
                    return true;
                  };

                  const dialogNodes = [];
                  const dialogs = Array.from(document.querySelectorAll('[role=dialog], [aria-modal=\"true\"]'));
                  if (dialogs.length) {
                    for (const dlg of dialogs) {
                      dialogNodes.push(
                        ...Array.from(
                          dlg.querySelectorAll('button, input, textarea, select, option, a, [role=button], [role=link], [onclick]')
                        )
                      );
                    }
                  }

                  // Элементы, которые Playwright уже нашел по selector
                  const pageNodes = Array.from(nodes);
                  const allNodes = dialogs.length ? [...dialogNodes, ...pageNodes] : pageNodes;

                  let id = 0;
                  const distilled = [];
                  for (const el of allNodes) {
                    if (!isVisible(el)) continue;
                    const agentId = `${stamp}-${id++}`;
                    el.setAttribute("data-agent-id", agentId);
                    const rect = el.getBoundingClientRect();
                    const position = `${Math.round(rect.top)}x${Math.round(rect.left)}`;
                    distilled.push({
                      agentId,
                      tag: (el.tagName || "").toLowerCase(),
                      role: el.getAttribute("role"),
                      inputType: el.type || null,
                      text: (el.innerText || el.value || "").trim().slice(0, 120),
                      placeholder: el.placeholder || null,
                      ariaLabel: el.getAttribute("aria-label") || null,
                      href: el.href || null,
                      location: position,
                    });
                    if (distilled.length >= 200) break;
                  }
                  return distilled;
                }
                """,
                arg=stamp,
            )
        except Exception as exc:  # noqa: BLE001
            return self._result(
                False,
                "analyze_page",
                "Failed to analyze page",
                error_type="AnalysisError",
                error=str(exc),
                suggestion="Try: 1) wait_for_element(); 2) close_modal(); 3) analyze_page() again.",
            )

        for idx, raw in enumerate(raw_elements):
            elements.append(
                DistilledElement(
                    id=idx,
                    agent_id=raw.get("agentId", ""),
                    tag=raw.get("tag", ""),
                    role=raw.get("role"),
                    input_type=raw.get("inputType"),
                    text=raw.get("text", ""),
                    placeholder=raw.get("placeholder"),
                    aria_label=raw.get("ariaLabel"),
                    href=raw.get("href"),
                    location=raw.get("location", ""),
                )
            )

        self.last_elements = elements

        if response_format == "concise":
            condensed = [
                {
                    "id": el.id,
                    "type": el.input_type or el.tag,
                    "text": el.text or el.aria_label or el.placeholder,
                    "location": el.location,
                }
                for el in elements[:20]
            ]
        else:
            condensed = [
                {
                    "id": el.id,
                    "type": el.input_type or el.tag,
                    "text": el.text,
                    "placeholder": el.placeholder,
                    "aria_label": el.aria_label,
                    "href": el.href,
                    "location": el.location,
                }
                for el in elements
            ]

        return self._result(
            True,
            "analyze_page",
            f"Distilled {len(elements)} elements",
            data={
                "page_title": self.page.title(),
                "url": self.page.url,
                "elements": condensed,
                "note": f"{len(elements)} elements distilled. Use click_element(id) / type_text(id, text).",
            },
        )

    def click_element(self, element_id: int) -> Dict:
        element = self._get_element(element_id)
        if not element:
            return self._result(
                False,
                "click_element",
                "Unknown element ID",
                error_type="ElementNotFound",
                error="Unknown element ID. Call analyze_page() first.",
                suggestion="Call analyze_page() to refresh IDs, then retry with the new ID.",
            )
        agent_selector = f'[data-agent-id="{element.agent_id}"]'
        locator = self.page.locator(agent_selector)

        def _ok(msg_suffix: str = "") -> Dict:
            time.sleep(0.8)
            self._sync_active_page()
            return self._result(
                True,
                "click_element",
                f"Clicked element {element_id}{msg_suffix}",
                data={"url": self.page.url, "element_id": element_id},
            )

        try:
            # Если элемент с таким data-agent-id исчез — страница перерисовалась
            if locator.count() == 0:
                snapshot = self.analyze_page(response_format="detailed")
                if not snapshot.get("success"):
                    return self._result(
                        False,
                        "click_element",
                        "Element disappeared and re-analyze_page() failed",
                        error_type="ClickError",
                        suggestion="Try analyze_page() manually and use search_elements()/click_text().",
                    )
                text = (element.text or "").strip()
                if text:
                    # Пытаемся найти свежий элемент с тем же текстом в новом дистилляте
                    for el in self.last_elements:
                        if (el.text or "").strip() == text:
                            new_locator = self.page.locator(f'[data-agent-id="{el.agent_id}"]').first
                            new_locator.click(timeout=5000)
                            return _ok(" (via refreshed agent-id)")
                    # fallback по видимому тексту
                    try:
                        fallback = self.page.get_by_text(text, exact=False).first
                        fallback.click(timeout=5000)
                        return _ok(" (via text fallback)")
                    except Exception as exc2:
                        return self._result(
                            False,
                            "click_element",
                            f"Failed to click element by text '{text}'",
                            error_type="ClickError",
                            error=str(exc2),
                            suggestion="Try scroll_page()/close_modal(), then analyze_page() and click_text() manually.",
                        )
                return self._result(
                    False,
                    "click_element",
                    "Element disappeared and has no stable text to match",
                    error_type="ElementNotFound",
                    suggestion="Run analyze_page() again and choose element by new ID.",
                )

            # Обычный клик по актуальному data-agent-id
            locator.first.click(timeout=5000)
            return _ok()

        except TimeoutError as exc:
            text = (element.text or "").strip()
            if text:
                try:
                    fallback = self.page.get_by_text(text, exact=False).first
                    fallback.click(timeout=5000)
                    return _ok(" (via text fallback after timeout)")
                except Exception as exc2:
                    return self._result(
                        False,
                        "click_element",
                        "Timeout clicking element (id & text fallback failed)",
                        error_type="Timeout",
                        error=f"{exc}; text_fallback_error={exc2}",
                        suggestion="Try scroll_page()/close_modal(), then analyze_page() and click_text().",
                    )
            return self._result(
                False,
                "click_element",
                "Timeout clicking element",
                error_type="Timeout",
                error=str(exc),
                suggestion="Try: 1) scroll_page(); 2) close_modal(); 3) analyze_page() and use click_text().",
            )
        except Exception as exc:  # noqa: BLE001
            return self._result(
                False,
                "click_element",
                "Failed to click element",
                error_type="ClickError",
                error=str(exc),
                suggestion="Try: 1) scroll_page(); 2) close_modal(); 3) analyze_page() and retry click_element.",
            )

    def type_text(self, element_id: int, text: str, press_enter: bool = False) -> Dict:
        element = self._get_element(element_id)
        if not element:
            return self._result(
                False,
                "type_text",
                "Unknown element ID",
                error_type="ElementNotFound",
                error="Unknown element ID. Call analyze_page() first.",
                suggestion="Call analyze_page() to refresh IDs, then retry with the new ID.",
            )
        locator = self.page.locator(f'[data-agent-id="{element.agent_id}"]')
        try:
            locator.fill(text, timeout=5000)
            if press_enter:
                locator.press("Enter")
            time.sleep(0.5)
            return self._result(
                True,
                "type_text",
                f"Typed into element {element_id}",
                data={"url": self.page.url, "element_id": element_id},
            )
        except TimeoutError as exc:
            return self._result(
                False,
                "type_text",
                "Timeout typing into element",
                error_type="Timeout",
                error=str(exc),
                suggestion="Try: 1) click_element() on the field; 2) close_modal(); 3) analyze_page() and retry type_text.",
            )
        except Exception as exc:  # noqa: BLE001
            return self._result(
                False,
                "type_text",
                "Failed to type into element",
                error_type="TypeError",
                error=str(exc),
                suggestion="Try: 1) click_element() on the input; 2) analyze_page() for a fresh ID; 3) retry type_text.",
            )

    def click_and_type(self, element_id: int, text: str, press_enter: bool = True) -> Dict:
        """Click then type in one step; useful for search fields."""
        click_res = self.click_element(element_id)
        if not click_res.get("success"):
            return click_res
        type_res = self.type_text(element_id, text, press_enter=press_enter)
        success = click_res.get("success") and type_res.get("success")
        return self._result(
            success,
            "click_and_type",
            "Clicked and typed",
            data={"click": click_res, "type": type_res},
            error_type=type_res.get("error_type") if not success else None,
            error=type_res.get("error") if not success else None,
            suggestion=type_res.get("suggestion") if not success else None,
        )

    def click_text(self, text: str, exact: bool = False) -> Dict:
        """Fallback: click by visible text when element is not in distillation."""
        try:
            locator = self.page.get_by_text(text, exact=exact).first
            locator.click(timeout=5000)
            time.sleep(0.5)
            self._sync_active_page()
            return self._result(
                True,
                "click_text",
                f"Clicked element with text '{text}'",
                data={"url": self.page.url, "text": text, "exact": exact},
            )
        except TimeoutError as exc:
            return self._result(
                False,
                "click_text",
                "Timeout clicking by text",
                error_type="Timeout",
                error=str(exc),
                suggestion="Попробуй: 1) scroll_page(); 2) уточни текст; 3) используй analyze_page() и click_element().",
            )
        except Exception as exc:  # noqa: BLE001
            return self._result(
                False,
                "click_text",
                "Failed to click by text",
                error_type="ClickError",
                error=str(exc),
                suggestion="Проверь, что такой текст реально виден на странице и не перекрыт модалкой.",
            )

    def navigate_url(self, url: str) -> Dict:
        target = url.strip()
        if not target.startswith(("http://", "https://")):
            target = f"https://{target}"
        try:
            self.page.goto(target, wait_until="load")
            self._sync_active_page()
            return self._result(True, "navigate_url", f"Navigated to {target}", data={"url": self.page.url})
        except TimeoutError as exc:
            return self._result(
                False,
                "navigate_url",
                "Timeout navigating",
                error_type="Timeout",
                error=str(exc),
                suggestion="Try: 1) wait_for_element(); 2) retry navigate_url(); 3) take_screenshot() for debugging.",
            )
        except Exception as exc:  # noqa: BLE001
            return self._result(
                False,
                "navigate_url",
                "Failed to navigate",
                error_type="NavigationError",
                error=str(exc),
                suggestion="Check URL format (use https://), then retry navigate_url().",
            )

    def take_screenshot(self, label: Optional[str] = None, embed_b64: bool = False) -> Dict:
        if not self.screenshot_dir and not embed_b64:
            return self._result(
                False,
                "take_screenshot",
                "Screenshot directory not configured and embed_b64 is False",
                error_type="ScreenshotError",
                suggestion="Set --screenshot-dir or call with embed_b64=True.",
            )
        self.screenshot_dir and self.screenshot_dir.mkdir(parents=True, exist_ok=True)
        slug = label.replace(" ", "_") if label else "shot"
        filename = f"{int(time.time())}_{slug}.png"
        path = self.screenshot_dir / filename if self.screenshot_dir else None
        try:
            raw_bytes = self.page.screenshot(path=str(path) if path else None, full_page=True)
            result_data: Dict[str, str] = {}
            if path:
                result_data["path"] = str(path)
            if embed_b64:
                b64 = base64.b64encode(raw_bytes).decode("ascii")
                # Trim to keep token usage reasonable for the LLM.
                max_len = 20000
                result_data["base64_png"] = b64[:max_len]
                if len(b64) > max_len:
                    result_data["note"] = "base64 truncated"
            return self._result(True, "take_screenshot", "Screenshot captured", data=result_data)
        except Exception as exc:  # noqa: BLE001
            return self._result(
                False,
                "take_screenshot",
                "Failed to take screenshot",
                error_type="ScreenshotError",
                error=str(exc),
                suggestion="Ensure page is loaded; retry take_screenshot().",
            )

    def wait_for_element(self, query: str, timeout: float = 5.0) -> Dict:
        try:
            self.page.get_by_text(query).first.wait_for(timeout=timeout * 1000)
            return self._result(
                True,
                "wait_for_element",
                f"Element containing '{query}' appeared",
                data={"query": query, "timeout": timeout},
            )
        except TimeoutError as exc:
            return self._result(
                False,
                "wait_for_element",
                f"Timed out after {timeout}s waiting for '{query}'",
                error_type="Timeout",
                error=str(exc),
                suggestion="Try: 1) increase timeout; 2) take_screenshot(); 3) analyze_page() and search_elements().",
            )

    def scroll_page(self, direction: str = "down", amount: int = 800) -> Dict:
        delta = abs(int(amount))
        dy = delta if direction == "down" else -delta
        try:
            self.page.mouse.wheel(0, dy)
            time.sleep(0.3)
            return self._result(
                True,
                "scroll_page",
                f"Scrolled {direction} by {delta}px",
                data={"direction": direction, "amount": delta},
            )
        except Exception as exc:  # noqa: BLE001
            return self._result(
                False,
                "scroll_page",
                "Failed to scroll",
                error_type="ScrollError",
                error=str(exc),
                suggestion="Try smaller amount, click on page to focus, then retry scroll_page().",
            )

    def switch_to_page(self, index: int = -1) -> Dict:
        if not self.session.context:
            return self._result(
                False,
                "switch_to_page",
                "No context",
                error_type="ContextError",
                suggestion="Ensure browser session is initialized.",
            )
        pages = self.session.context.pages
        if not pages:
            return self._result(
                False,
                "switch_to_page",
                "No open pages",
                error_type="PageSwitchError",
                suggestion="No tabs to switch; stay on current page.",
            )
        try:
            self.session.page = pages[index]
            return self._result(
                True,
                "switch_to_page",
                f"Focused page {index}",
                data={"url": self.session.page.url, "index": index},
            )
        except Exception as exc:  # noqa: BLE001
            return self._result(
                False,
                "switch_to_page",
                "Failed to switch page",
                error_type="PageSwitchError",
                error=str(exc),
                suggestion="Check tab index; use index=-1 for newest tab.",
            )

    def go_back(self) -> Dict:
        try:
            resp = self.page.go_back(wait_until="load", timeout=8000)
            self._sync_active_page()
            msg = "Navigated back" if resp else "No history to go back"
            return self._result(True, "go_back", msg, data={"url": self.page.url})
        except Exception as exc:  # noqa: BLE001
            return self._result(
                False,
                "go_back",
                "Failed to go back",
                error_type="NavigationError",
                error=str(exc),
                suggestion="Try navigate_url() to desired page if history is broken.",
            )

    def close_modal(self) -> Dict:
        """Attempt to close common modal/overlay dialogs by clicking close buttons or pressing Escape."""
        selectors = [
            "[role=dialog] button[aria-label*='close' i]",
            "[aria-modal='true'] button[aria-label*='close' i]",
            "[data-testid*='close' i]",
            "button:has-text('×')",
            "button:has-text('✕')",
            "button:has-text('Закрыть')",
            "[role=dialog] button",
            "[aria-modal='true'] button",
        ]
        tried = []
        for sel in selectors:
            try:
                btn = self.page.locator(sel).first
                if btn.count() == 0:
                    tried.append({"selector": sel, "found": False})
                    continue
                btn.click(timeout=2000)
                self._sync_active_page()
                return self._result(
                    True,
                    "close_modal",
                    f"Closed modal via selector '{sel}'",
                    data={"tried": tried},
                )
            except Exception as exc:  # noqa: BLE001
                tried.append({"selector": sel, "error": str(exc)})
                continue
        # Fallback: press Escape
        try:
            self.page.keyboard.press("Escape")
            return self._result(
                True,
                "close_modal",
                "Pressed Escape to close modal",
                data={"tried": tried},
            )
        except Exception as exc:  # noqa: BLE001
            tried.append({"selector": "Escape", "error": str(exc)})
            return self._result(
                False,
                "close_modal",
                "Failed to close modal",
                error_type="ModalError",
                error=str(exc),
                suggestion="Try: 1) scroll_page(); 2) analyze_page() and click close manually; 3) retry close_modal().",
                data={"tried": tried},
            )

    def search_elements(self, query: str, max_results: int = 5) -> Dict:
        if not self.last_elements:
            self.analyze_page(response_format="detailed")
        matches: List[DistilledElement] = []
        q = query.lower()
        for el in self.last_elements:
            haystack = " ".join(
                [
                    el.text or "",
                    el.placeholder or "",
                    el.aria_label or "",
                    el.href or "",
                ]
            ).lower()
            if q in haystack:
                matches.append(el)
            if len(matches) >= max_results:
                break

        return self._result(
            True,
            "search_elements",
            f"Found {len(matches)} results for '{query}'",
            data={
                "query": query,
                "results": [
                    {
                        "id": el.id,
                        "type": el.input_type or el.tag,
                        "text": el.text or el.aria_label or el.placeholder,
                        "location": el.location,
                    }
                    for el in matches
                ],
                "note": "IDs are from the latest analyze_page() run.",
            },
        )

    def query_dom(self, question: str) -> Dict:
        """
        Ответить на уточняющий вопрос о текущей странице.
        Делает свежий analyze_page(response_format="detailed"), затем передает снимок в DOM-подагент.
        """
        snapshot_result = self.analyze_page(response_format="detailed")
        if not snapshot_result.get("success"):
            return self._result(
                False,
                "query_dom",
                "Не удалось получить снимок страницы для анализа",
                error_type="AnalysisError",
                error=snapshot_result.get("error"),
                suggestion="Посмотри на результат analyze_page(), попробуй scroll_page()/wait_for_element()/close_modal() и повтори.",
                data={"snapshot": snapshot_result},
            )

        snapshot_data = snapshot_result.get("data", {})
        dom_agent = getattr(self, "dom_agent", None)
        if dom_agent is None:
            return self._result(
                False,
                "query_dom",
                "DOM-подагент не инициализирован",
                error_type="DomAgentError",
                suggestion="Убедись, что DomSubAgent создаётся в BrowserAgent и присваивается executor.dom_agent.",
                data={"snapshot": snapshot_data},
            )

        answer = dom_agent.answer(question, snapshot_data)
        return self._result(
            True,
            "query_dom",
            "Ответ на вопрос о DOM получен",
            data={
                "question": question,
                "answer": answer,
                "page_title": snapshot_data.get("page_title"),
                "url": snapshot_data.get("url"),
            },
        )

    def validate_task_complete(self, hint: Optional[str] = None) -> Dict:
        return self._result(
            True,
            "validate_task_complete",
            "Returned page summary",
            data={
                "page_title": self.page.title(),
                "url": self.page.url,
                "text_sample": self.page.text_content("body")[:2000] if self.page else "",
                "hint": hint or "Check text_sample for success signals (cart count, confirmation).",
            },
        )

    def extract_text(self, selector: str, all_matches: bool = False, max_chars: int = 4000) -> Dict:
        """Extract visible text from selector(s); useful for email bodies, job cards, etc."""
        try:
            if all_matches:
                texts = self.page.locator(selector).all_inner_texts()
                joined = "\n\n---\n\n".join(texts)
                return self._result(
                    True,
                    "extract_text",
                    "Extracted texts",
                    data={"text": joined[:max_chars], "truncated": len(joined) > max_chars},
                )
            text = self.page.locator(selector).inner_text(timeout=5000)
            return self._result(
                True,
                "extract_text",
                "Extracted text",
                data={"text": text[:max_chars], "truncated": len(text) > max_chars},
            )
        except TimeoutError as exc:
            return self._result(
                False,
                "extract_text",
                "Timeout extracting text",
                error_type="Timeout",
                error=str(exc),
                suggestion="Try: 1) scroll_page(); 2) ensure selector is visible; 3) retry extract_text().",
            )
        except Exception as exc:  # noqa: BLE001
            return self._result(
                False,
                "extract_text",
                "Failed to extract text",
                error_type="ExtractError",
                error=str(exc),
                suggestion="Verify selector exists; call analyze_page() for context and retry.",
            )

    def collect_elements(self, selector: str, limit: int = 20) -> Dict:
        """Return list of elements' text/aria/links for richer lists (emails, products, vacancies)."""
        try:
            data = self.page.eval_on_selector_all(
                selector,
                """
                (nodes, limit) => {
                  const res = [];
                  for (const el of nodes) {
                    const rect = el.getBoundingClientRect();
                    const visible = rect && rect.width > 0 && rect.height > 0;
                    if (!visible) continue;
                    const text = (el.innerText || '').trim();
                    const href = el.href || el.getAttribute('href') || null;
                    const aria = el.getAttribute('aria-label') || null;
                    const title = el.getAttribute('title') || null;
                    res.push({ text, href, aria, title });
                    if (res.length >= limit) break;
                  }
                  return res;
                }
                """,
                limit,
            )
            return self._result(
                True,
                "collect_elements",
                f"Collected {len(data)} items",
                data={"items": data, "count": len(data)},
            )
        except Exception as exc:  # noqa: BLE001
            return self._result(
                False,
                "collect_elements",
                "Failed to collect elements",
                error_type="CollectError",
                error=str(exc),
                suggestion="Try: 1) scroll_page(); 2) verify selector; 3) analyze_page() for structure, then retry.",
            )

    def switch_frame(self, selector: Optional[str] = None, index: int = 0) -> Dict:
        """Focus an iframe by CSS selector or index; selector has priority. Pass selector=None,index=-1 for main frame."""
        try:
            if selector is None and index == -1:
                self.page.main_frame().goto(self.page.url)
                return self._result(True, "switch_frame", "Switched to main frame", data={"url": self.page.url})
            if selector:
                handle = self.page.query_selector(selector)
                if not handle:
                    return self._result(
                        False,
                        "switch_frame",
                        "Frame not found",
                        error_type="FrameError",
                        error="Frame not found by selector",
                        suggestion="Call analyze_page() to locate iframe; verify selector and retry.",
                    )
                frame = handle.content_frame()
            else:
                frames = self.page.frames
                if index < 0 or index >= len(frames):
                    return self._result(
                        False,
                        "switch_frame",
                        "Frame index out of range",
                        error_type="FrameError",
                        suggestion="Check number of frames; use index=-1 to return to main frame.",
                    )
                frame = frames[index]
            if not frame:
                return self._result(
                    False,
                    "switch_frame",
                    "Content frame not available",
                    error_type="FrameError",
                    suggestion="Frame exists but not ready; wait_for_element() then retry.",
                )
            self.session.page = frame.page
            return self._result(True, "switch_frame", "Switched frame", data={"url": self.session.page.url})
        except Exception as exc:  # noqa: BLE001
            return self._result(
                False,
                "switch_frame",
                "Failed to switch frame",
                error_type="FrameError",
                error=str(exc),
                suggestion="Try: 1) wait_for_element(); 2) close_modal(); 3) analyze_page() to confirm iframe.",
            )

    def _get_element(self, element_id: int) -> Optional[DistilledElement]:
        for el in self.last_elements:
            if el.id == element_id:
                return el
        return None
