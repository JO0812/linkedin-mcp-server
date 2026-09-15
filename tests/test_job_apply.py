"""Tests for the human-gated Easy Apply tools in tools/job_apply.py.

No network, no real browser, no LinkedIn. The page/extractor fakes below
imitate only OUR selector/walk logic (which locator strings the code reads
and which awaited methods it calls), never LinkedIn's markup.

Spec case map (see task): 1-4 form detection, 5-17 answer proposals,
18-21 submit gates, 22-24 prepare, 25-30 modal discovery, 31-32 apply log,
33-35 registration, 36 TOOL_BUILD.
"""

import json
import re
from datetime import datetime
from typing import Any, Callable, Coroutine, cast
from unittest.mock import AsyncMock  # noqa: F401  (documents the mock style)
from unittest.mock import MagicMock  # noqa: F401  (documents the mock style)

import pytest
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.tools import FunctionTool

import linkedin_mcp_server.tools.job_apply as ja
from linkedin_mcp_server.core.exceptions import ScrapingError


async def get_tool_fn(
    mcp: FastMCP, name: str
) -> Callable[..., Coroutine[Any, Any, dict[str, Any]]]:
    """Extract tool function from FastMCP by name using public API."""
    tool = await mcp.get_tool(name)
    assert tool is not None, f"Tool '{name}' not found"
    return cast(FunctionTool, tool).fn


_shared_mcp: FastMCP | None = None


def _fresh_mcp() -> FastMCP:
    """One shared registration: the tools are stateless over the module."""
    global _shared_mcp
    if _shared_mcp is None:
        _shared_mcp = FastMCP("test")
        ja.register_apply_tools(_shared_mcp)
    return _shared_mcp


def _read_log(path: Any) -> list[dict[str, Any]]:
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


# ---------------------------------------------------------------------------
# Scripted fakes. They expose exactly the awaited methods the code reads,
# with the semantics the code relies on (count() -> int, inner_text() -> str,
# click()/fill()/select_option()/set_input_files() record the call,
# is_visible/is_enabled -> configured flags).


def _matches_text(el: Any, pattern: Any) -> bool:
    text = getattr(el, "_text", "") or ""
    if hasattr(pattern, "search"):
        return bool(pattern.search(text))
    return str(pattern) in text


def _matches_name(el: Any, pattern: Any) -> bool:
    """Accessible-name match: aria-label wins over content, like Playwright.

    get_by_role folds aria-label into ONE accessible name (the label takes
    precedence); matching text and aria separately with OR would let an
    empty-match pattern slip through on an empty-text button.
    """
    aria = getattr(el, "_aria", None) or ""
    text = getattr(el, "_text", "") or ""
    aname = aria if aria else text
    if hasattr(pattern, "search"):
        return bool(pattern.search(aname))
    return str(pattern) in aname


class _Empty:
    """Null element/locator: every awaited method is a benign no-op."""

    @property
    def first(self) -> "_Empty":
        return self

    def nth(self, i: int) -> Any:
        raise IndexError(i)

    def filter(self, has_text: Any = None) -> "_Empty":
        return self

    def get_by_role(self, role: str, name: Any = None) -> "_Empty":
        return self

    def locator(self, selector: str) -> "_Empty":
        return self

    async def count(self) -> int:
        return 0

    async def inner_text(self, timeout: Any = None) -> str:
        return ""

    async def get_attribute(self, name: str) -> Any:
        return None

    async def is_visible(self) -> bool:
        return False

    async def is_enabled(self) -> bool:
        return False

    async def scroll_into_view_if_needed(self, timeout: Any = None) -> None:
        return None

    async def click(self, timeout: Any = None) -> None:
        return None

    async def fill(self, value: str, timeout: Any = None) -> None:
        return None

    async def select_option(self, label: Any = None, timeout: Any = None) -> None:
        return None

    async def set_input_files(self, path: str, timeout: Any = None) -> None:
        return None

    async def all_inner_texts(self) -> list[str]:
        return []


_EMPTY = _Empty()


class _Loc:
    """Locator over a fixed element list with filter/get_by_role support."""

    def __init__(self, elements: list[Any]):
        self._els = list(elements)

    @property
    def first(self) -> Any:
        return self._els[0] if self._els else _EMPTY

    def nth(self, i: int) -> Any:
        return self._els[i]

    def filter(self, has_text: Any = None) -> "_Loc":
        if has_text is None:
            return _Loc(list(self._els))
        return _Loc([e for e in self._els if _matches_text(e, has_text)])

    def get_by_role(self, role: str, name: Any = None) -> "_Loc":
        out = []
        for e in self._els:
            er = getattr(e, "_role", None)
            if er is not None and er != role:
                continue
            if name is not None and not _matches_name(e, name):
                continue
            out.append(e)
        return _Loc(out)

    def locator(self, selector: str) -> "_Loc":
        return _Loc([])

    async def count(self) -> int:
        return len(self._els)

    async def all_inner_texts(self) -> list[str]:
        return [getattr(e, "_text", "") or "" for e in self._els]


class _El:
    """Single element: configured flags, recording actions."""

    def __init__(
        self,
        text: str = "",
        *,
        aria: str | None = None,
        role: str | None = None,
        visible: bool = True,
        enabled: bool = True,
        on_click: Callable[[], None] | None = None,
    ):
        self._text = text
        self._aria = aria
        self._role = role
        self._visible = visible
        self._enabled = enabled
        self._on_click = on_click
        self.clicked = False
        self.click_count = 0

    @property
    def first(self) -> "_El":
        return self

    def nth(self, i: int) -> "_El":
        if i == 0:
            return self
        raise IndexError(i)

    def filter(self, has_text: Any = None) -> _Loc:
        return _Loc([self]).filter(has_text=has_text)

    def get_by_role(self, role: str, name: Any = None) -> _Loc:
        return _Loc([self]).get_by_role(role, name)

    def locator(self, selector: str) -> _Loc:
        return _Loc([])

    async def count(self) -> int:
        return 1

    async def inner_text(self, timeout: Any = None) -> str:
        return self._text

    async def get_attribute(self, name: str) -> Any:
        if name == "aria-label":
            return self._aria
        if name == "role":
            return self._role
        return None

    async def is_visible(self) -> bool:
        return self._visible

    async def is_enabled(self) -> bool:
        return self._enabled

    async def scroll_into_view_if_needed(self, timeout: Any = None) -> None:
        return None

    async def click(self, timeout: Any = None) -> None:
        self.clicked = True
        self.click_count += 1
        if self._on_click is not None:
            self._on_click()

    async def fill(self, value: str, timeout: Any = None) -> None:
        return None

    async def select_option(self, label: Any = None, timeout: Any = None) -> None:
        return None

    async def set_input_files(self, path: str, timeout: Any = None) -> None:
        return None

    async def all_inner_texts(self) -> list[str]:
        return [self._text]


class _SelectEl(_El):
    """Block-level <select>: exposes its options via locator("option")."""

    def __init__(self, options: list[str]):
        super().__init__(text="")
        self._options = list(options)

    def locator(self, selector: str) -> _Loc:
        if selector == "option":
            return _Loc([_El(text=o) for o in self._options])
        return _Loc([])


class _Block(_El):
    """One label/fieldset block wrapping a scripted question dict."""

    def __init__(self, question: dict[str, Any], page: "_FakePage"):
        super().__init__(text=question["label"])
        self._q = question
        self._page = page

    def locator(self, selector: str) -> _Loc:
        qtype = self._q["type"]
        options = self._q.get("options", [])
        if selector == "select":
            return _Loc([_SelectEl(options)]) if qtype == "select" else _Loc([])
        if selector == "input[type='radio']":
            return _Loc([_El(text="radio")]) if qtype == "radio" else _Loc([])
        if selector == "input[type='file']":
            return _Loc([_El(text="file")]) if qtype == "file" else _Loc([])
        if selector == "textarea":
            return _Loc([_El(text="")]) if qtype == "textarea" else _Loc([])
        if "input[type='text']" in selector:
            if qtype in ("text", "textarea"):
                return _Loc([_El(text="")])
            return _Loc([])
        if selector == "label":
            return _Loc([_El(text=o) for o in options])
        return _Loc([])


class _FillText(_El):
    def __init__(self, page: "_FakePage", label: str):
        super().__init__(text="")
        self._page = page
        self._label = label

    async def fill(self, value: str, timeout: Any = None) -> None:
        self._page.fill_calls.append(("text", self._label, value))


class _FillSelect(_El):
    def __init__(self, page: "_FakePage", label: str):
        super().__init__(text="")
        self._page = page
        self._label = label

    async def select_option(self, label: Any = None, timeout: Any = None) -> None:
        self._page.fill_calls.append(("select", self._label, label))


class _RadioOpt(_El):
    def __init__(self, page: "_FakePage", label: str, text: str):
        super().__init__(text=text)
        self._page = page
        self._label = label

    async def click(self, timeout: Any = None) -> None:
        self.clicked = True
        self.click_count += 1
        self._page.fill_calls.append(("radio", self._label, self._text))


class _CvInput(_El):
    def __init__(self, page: "_FakePage"):
        super().__init__(text="")
        self._page = page

    async def set_input_files(self, path: str, timeout: Any = None) -> None:
        self._page.cv_uploads.append(path)


class _Row(_El):
    """One div/fieldset row wrapping a scripted question (for _fill_field)."""

    def __init__(self, question: dict[str, Any], page: "_FakePage"):
        super().__init__(text=question["label"])
        self._q = question
        self._page = page

    def locator(self, selector: str) -> _Loc:
        qtype = self._q["type"]
        options = self._q.get("options", [])
        label = self._q["label"]
        if selector == "select":
            return (
                _Loc([_FillSelect(self._page, label)])
                if qtype == "select"
                else _Loc([])
            )
        if selector == "input[type='radio']":
            return _Loc([_El(text="radio")]) if qtype == "radio" else _Loc([])
        if selector == "label":
            return _Loc([_RadioOpt(self._page, label, o) for o in options])
        if "input[type='text']" in selector:
            if qtype in ("text", "textarea"):
                return _Loc([_FillText(self._page, label)])
            return _Loc([])
        return _Loc([])


class _DialogRoot(_El):
    """The open dialog element; children come from the scripted questions."""

    def __init__(self, page: "_FakePage"):
        super().__init__(text=page._dialog_text or "")
        self._page = page

    async def inner_text(self, timeout: Any = None) -> str:
        return self._page._dialog_text or ""

    def locator(self, selector: str) -> _Loc:
        p = self._page
        if selector == "label, fieldset":
            return _Loc([_Block(q, p) for q in p._questions])
        if selector == "button":
            return _Loc(list(p._dialog_buttons))
        if selector == "div, fieldset":
            return _Loc([_Row(q, p) for q in p._questions])
        if selector == "input[type='file']":
            return _Loc([p._cv_input] if p._cv_input is not None else [])
        return _Loc([])


class _Keyboard:
    def __init__(self, page: "_FakePage"):
        self._page = page
        self.presses: list[str] = []

    async def press(self, key: str) -> None:
        self.presses.append(key)
        if key == "Escape" and self._page._dismiss_on_escape:
            self._page._dialog_text = None


class _FakePage:
    """Scripted page exposing exactly what job_apply.py awaits."""

    def __init__(
        self,
        *,
        body: str = "",
        dialog_text: str | None = None,
        buttons: list[_El] | None = None,
        questions: list[dict[str, Any]] | None = None,
        goto_error: Exception | None = None,
        title: str = "Empleo | LinkedIn",
        url: str = "https://www.linkedin.com/jobs/view/123/",
        dismiss_on_escape: bool = False,
        lazy_buttons: list[_El] | None = None,
        cv_upload: bool = False,
    ):
        self._body = body
        self._dialog_text = dialog_text
        self._main_buttons: list[_El] = list(buttons or [])
        self._lazy_buttons: list[_El] | None = (
            list(lazy_buttons) if lazy_buttons else None
        )
        self._questions = list(questions or [])
        self._dialog_buttons: list[_El] = []
        self._goto_error = goto_error
        self._title = title
        self.url = url
        self._dismiss_on_escape = dismiss_on_escape
        self._cv_input = _CvInput(self) if cv_upload else None
        self.keyboard = _Keyboard(self)
        self.goto_calls: list[str] = []
        self.locator_calls: list[str] = []
        self.evaluate_calls: list[str] = []
        self.waits: list[int] = []
        self.screenshots: list[Any] = []
        self.apply_clicks: list[str] = []
        self.fill_calls: list[tuple[str, str, Any]] = []
        self.cv_uploads: list[str] = []

    def locator(self, selector: str) -> _Loc:
        self.locator_calls.append(selector)
        if selector == ja._DIALOG:
            if self._dialog_text is not None:
                return _Loc([_DialogRoot(self)])
            return _Loc([])
        if selector == "main":
            return _Loc(list(self._main_buttons))
        if selector.startswith("main button"):
            return _Loc(list(self._main_buttons))
        if selector == "button":
            return _Loc([])
        return _Loc([])

    async def goto(self, url: str, wait_until: Any = None, timeout: Any = None) -> None:
        self.goto_calls.append(url)
        if self._goto_error is not None:
            raise self._goto_error

    async def wait_for_selector(self, selector: str, timeout: Any = None) -> None:
        return None

    async def evaluate(self, js: str) -> str:
        self.evaluate_calls.append(js)
        return self._body

    async def title(self) -> str:
        return self._title

    async def wait_for_timeout(self, ms: int) -> None:
        self.waits.append(ms)
        if self._lazy_buttons is not None and ms == 1500:
            # Lazy job card: the apply control renders after the first retry.
            self._main_buttons.extend(self._lazy_buttons)
            self._lazy_buttons = None

    async def screenshot(self, path: Any = None, **kwargs: Any) -> bytes:
        self.screenshots.append(path)
        return b""


class _FakeExtractor:
    def __init__(self, page: _FakePage):
        self._page = page


def _apply_button(
    page: _FakePage, text: str = "Solicitar", aria: str | None = None
) -> _El:
    """An apply control whose click records and opens the modal."""

    def _open() -> None:
        page.apply_clicks.append(text or aria or "")
        page._dialog_text = "apply form"

    return _El(text=text, aria=aria, role="button", on_click=_open)


def _q(
    label: str, qtype: str = "text", options: list[str] | None = None
) -> dict[str, Any]:
    return {"label": label, "type": qtype, "options": options or []}


# ---------------------------------------------------------------------------


class TestLooksLikeApplyForm:
    def test_search_history_popup_is_not_a_form(self) -> None:
        # Case 1: the live-blocker regression — suggestions popup text.
        popup = "9 sugerencias disponibles. Marianne Escaff METRICA Chile"
        assert ja._looks_like_apply_form(popup) is False

    def test_spanish_easy_apply_form(self) -> None:
        # Case 2: real Easy Apply form text (Spanish).
        form = "¿Cuántos años de experiencia tienes? Teléfono Adjunta tu curriculum"
        assert ja._looks_like_apply_form(form) is True

    def test_english_easy_apply_form(self) -> None:
        # Case 3: real Easy Apply form text (English).
        form = "How many years of experience do you have? Upload your resume Full name"
        assert ja._looks_like_apply_form(form) is True

    def test_empty_string(self) -> None:
        # Case 4.
        assert ja._looks_like_apply_form("") is False


class TestProposeAnswer:
    def test_phone_es(self) -> None:
        # Case 5.
        out = ja._propose_answer("Teléfono", "text", [], {"phone": "+56912345678"})
        assert out["value"] == "+56912345678"
        assert out["needs_user"] is False
        assert out["source"] == "profile"

    def test_email_en(self) -> None:
        # Case 6.
        out = ja._propose_answer("Email address", "text", [], {"email": "a@b.cl"})
        assert out["value"] == "a@b.cl"
        assert out["needs_user"] is False
        assert out["source"] == "profile"

    def test_location_es(self) -> None:
        # Case 7.
        out = ja._propose_answer(
            "¿En qué ciudad te encuentras?", "text", [], {"location": "Santiago, Chile"}
        )
        assert out["value"] == "Santiago, Chile"
        assert out["needs_user"] is False
        assert out["source"] == "profile"

    def test_authorization_en(self) -> None:
        # Case 8.
        out = ja._propose_answer(
            "Are you legally authorized to work?",
            "select",
            ["Yes", "No"],
            {"work_authorization_statement": "Authorized, no sponsorship needed"},
        )
        assert out["value"] == "Authorized, no sponsorship needed"
        assert out["needs_user"] is False
        assert out["source"] == "profile"

    def test_salary_es(self) -> None:
        # Case 9.
        out = ja._propose_answer(
            "¿Cuáles son tus pretensiones de renta?",
            "text",
            [],
            {"salary_statement": "CLP 3M líquidos"},
        )
        assert out["value"] == "CLP 3M líquidos"
        assert out["needs_user"] is False
        assert out["source"] == "profile"

    # Case 10 covered by test_notice_es_phrase (keyword table fixed to match "¿Cuándo puedes comenzar?").

    def test_notice_es_phrase(self) -> None:
        profile = {"notice_statement": "30 días de preaviso; disponibilidad inmediata"}
        out = ja._propose_answer("¿Cuándo puedes comenzar?", "text", [], profile)
        assert out["value"] == "30 días de preaviso; disponibilidad inmediata"
        assert out["needs_user"] is False
        assert out["source"] == "profile"
        neg = ja._propose_answer("¿Cuándo supiste de esta oferta?", "text", [], profile)
        assert neg["needs_user"] is True

    def test_years_select_default(self) -> None:
        # Case 11: user-approved default, even with options and empty profile.
        out = ja._propose_answer(
            "¿Cuántos años de experiencia tienes?",
            "select",
            ["1", "2", "3", "5"],
            {},
        )
        assert out["value"] == "3"
        assert out["needs_user"] is False

    def test_years_textarea_no_rule(self) -> None:
        # Case 12.
        out = ja._propose_answer("Describe your experience", "textarea", [], {})
        assert out["needs_user"] is True
        assert out["value"] is None
        assert "descriptive" in out["reason"]

    def test_years_textarea_matching_rule(self) -> None:
        # Case 13.
        profile = {
            "years_statements": [{"match": "python", "value": "5 years of Python"}]
        }
        out = ja._propose_answer(
            "Describe your Python experience", "textarea", [], profile
        )
        assert out["value"] == "5 years of Python"
        assert out["needs_user"] is False
        assert out["source"] == "profile"

    def test_constrained_select_needs_user(self) -> None:
        # Case 14: never auto-picked.
        out = ja._propose_answer(
            "What is your highest degree?", "select", ["Bachelor", "Master"], {}
        )
        assert out["needs_user"] is True
        assert out["value"] is None
        assert "constrain" in out["reason"].lower() or "choice" in out["reason"].lower()

    def test_constrained_radio_needs_user(self) -> None:
        # Case 15.
        out = ja._propose_answer("Are you a veteran?", "radio", ["Yes", "No"], {})
        assert out["needs_user"] is True
        assert out["value"] is None

    def test_years_select_respects_profile(self) -> None:
        # Case 16.
        out = ja._propose_answer(
            "¿Cuántos años de experiencia tienes?",
            "select",
            ["1", "5"],
            {"default_years": 5},
        )
        assert out["value"] == "5"
        assert out["needs_user"] is False

    def test_unknown_label_needs_user(self) -> None:
        # Case 17.
        out = ja._propose_answer("¿Cuál es tu color favorito?", "text", [], {})
        assert out["needs_user"] is True
        assert out["value"] is None
        assert "no profile fact" in out["reason"]


class TestSubmitGates:
    async def test_confirm_false_aborts_untouched(
        self, mock_context: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        # Case 18: the page-untouched contract.
        log = tmp_path / "apply-log.jsonl"
        monkeypatch.setattr(ja, "APPLY_LOG", str(log))
        page = _FakePage(
            body="Solicitud sencilla",
            buttons=[_El(text="Solicitar", role="button")],
        )
        tool_fn = await get_tool_fn(_fresh_mcp(), "submit_application")
        result = await tool_fn(
            "123",
            '[{"label": "A", "value": "B"}]',
            "",
            False,
            mock_context,
            extractor=_FakeExtractor(page),
        )
        assert result["status"] == "aborted"
        assert "confirm_send" in result["reason"]
        assert result["confirm_send"] is False
        assert page.goto_calls == []
        assert page.locator_calls == []
        assert page.evaluate_calls == []
        entries = _read_log(log)
        assert len(entries) == 1
        assert entries[0]["status"] == "aborted"
        assert entries[0]["job_id"] == "123"
        assert entries[0]["confirm_send"] is False
        assert "ts" in entries[0]

    async def test_malformed_answers_json(
        self, mock_context: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        # Case 19.
        log = tmp_path / "apply-log.jsonl"
        monkeypatch.setattr(ja, "APPLY_LOG", str(log))
        page = _FakePage(body="Solicitud sencilla")
        tool_fn = await get_tool_fn(_fresh_mcp(), "submit_application")
        result = await tool_fn(
            "123", "not json{", "", True, mock_context, extractor=_FakeExtractor(page)
        )
        assert result["status"] == "aborted"
        assert "answers_json" in result["reason"]
        assert page.goto_calls == []
        assert page.locator_calls == []
        assert _read_log(log)[0]["status"] == "aborted"

    async def test_not_easy_apply_aborts_without_submit(
        self, mock_context: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        # Case 20: body lacks Easy Apply markers.
        log = tmp_path / "apply-log.jsonl"
        monkeypatch.setattr(ja, "APPLY_LOG", str(log))
        page = _FakePage(
            body="Ingeniero de software. Postular en el sitio web de la empresa.",
            buttons=[_El(text="Postular en el sitio", role="button")],
        )
        tool_fn = await get_tool_fn(_fresh_mcp(), "submit_application")
        result = await tool_fn(
            "123",
            '[{"label": "A", "value": "B"}]',
            "",
            True,
            mock_context,
            extractor=_FakeExtractor(page),
        )
        assert result["status"] == "aborted"
        assert "Easy Apply" in result["reason"]
        assert page.apply_clicks == []
        assert page.fill_calls == []
        assert all(not b.clicked for b in page._main_buttons)
        assert _read_log(log)[0]["status"] == "aborted"

    async def test_missing_required_aborts_and_closes_modal(
        self, mock_context: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        # Case 21: full submit to the missing_required abort.
        log = tmp_path / "apply-log.jsonl"
        monkeypatch.setattr(ja, "APPLY_LOG", str(log))
        page = _FakePage(
            body="Ingeniero de software. Solicitud sencilla.",
            questions=[_q("Teléfono *"), _q("¿Años de experiencia? *")],
        )
        page._main_buttons.append(_apply_button(page))
        tool_fn = await get_tool_fn(_fresh_mcp(), "submit_application")
        result = await tool_fn(
            "123",
            '[{"label": "Teléfono *", "value": "+56912345678"}]',
            "",
            True,
            mock_context,
            extractor=_FakeExtractor(page),
        )
        assert result["status"] == "aborted"
        assert "¿Años de experiencia? *" in result["missing_required"]
        assert ("text", "Teléfono *", "+56912345678") in page.fill_calls
        assert "Escape" in page.keyboard.presses
        entry = _read_log(log)[0]
        assert entry["status"] == "aborted"
        assert "¿Años de experiencia? *" in entry["missing_required"]


class TestPrepareApplication:
    def _profile(self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        prof = tmp_path / "apply-profile.yml"
        prof.write_text("phone: '+56912345678'\ndefault_years: 3\n", encoding="utf-8")
        monkeypatch.setattr(ja, "PROFILE_PATH", str(prof))
        monkeypatch.setattr(ja, "APPLY_LOG", str(tmp_path / "apply-log.jsonl"))

    async def test_draft_ready_happy_path(
        self, mock_context: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        # Case 22.
        self._profile(tmp_path, monkeypatch)
        page = _FakePage(
            body="Ingeniero de software. Solicitud sencilla.",
            questions=[
                _q("Teléfono *"),
                _q(
                    "¿Cuántos años de experiencia tienes? *",
                    "select",
                    ["Menos de 1 año", "1 a 3 años", "Más de 5 años"],
                ),
            ],
        )
        page._main_buttons.append(_apply_button(page))
        tool_fn = await get_tool_fn(_fresh_mcp(), "prepare_application")
        result = await tool_fn("123", mock_context, extractor=_FakeExtractor(page))
        assert result["status"] == "draft_ready"
        assert result["submitted"] is False
        assert len(result["questions"]) == 2
        assert result["questions"][0]["value"] == "+56912345678"
        assert result["questions"][1]["value"] == "3"
        assert "Escape" in page.keyboard.presses
        entries = _read_log(tmp_path / "apply-log.jsonl")
        assert len(entries) == 1
        assert entries[0]["tool"] == "prepare_application"
        assert entries[0]["status"] == "draft_ready"
        assert entries[0]["question_count"] == 2

    async def test_already_applied_cannot_prepare(
        self, mock_context: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        # Case 23.
        self._profile(tmp_path, monkeypatch)
        page = _FakePage(body="Ya has solicitado este empleo.")
        tool_fn = await get_tool_fn(_fresh_mcp(), "prepare_application")
        result = await tool_fn("123", mock_context, extractor=_FakeExtractor(page))
        assert result["status"] == "cannot_prepare"
        assert "already applied" in result["reason"]
        assert result.get("submitted", False) is False
        assert _read_log(tmp_path / "apply-log.jsonl")[0]["status"] == "cannot_prepare"

    async def test_page_failure_raises_and_logs_error(
        self, mock_context: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        # Case 24: goto blows up -> logged as error, then raise_tool_error.
        self._profile(tmp_path, monkeypatch)
        page = _FakePage(goto_error=ScrapingError("boom"))
        tool_fn = await get_tool_fn(_fresh_mcp(), "prepare_application")
        with pytest.raises(ToolError):
            await tool_fn("123", mock_context, extractor=_FakeExtractor(page))
        assert _read_log(tmp_path / "apply-log.jsonl")[0]["status"] == "error"


class TestOpenEasyApply:
    async def test_aria_label_only_button(self) -> None:
        # Case 25: THE aria-label case — empty text, name only in aria-label.
        page = _FakePage(body="Ingeniero. Solicitud sencilla.")
        btn = _apply_button(page, text="", aria="Solicitar")
        page._main_buttons.append(btn)
        out = await ja._open_easy_apply(_FakeExtractor(page), "123")
        assert out["ok"] is True
        assert btn.clicked is True

    async def test_lazy_button_retry(self) -> None:
        # Case 26: control renders only after the first discovery attempt.
        page = _FakePage(body="Ingeniero. Solicitud sencilla.")
        btn = _apply_button(page)
        page._lazy_buttons = [btn]
        out = await ja._open_easy_apply(_FakeExtractor(page), "123")
        assert out["ok"] is True
        assert any(w == 1500 for w in page.waits), "retry loop never waited"
        assert btn.clicked is True

    async def test_no_button_reports_diagnostics(self) -> None:
        # Case 27: 72 nav buttons, none matching; combobox popup dismissed.
        page = _FakePage(
            body="Oferta de empleo: Ingeniero. Solicitud sencilla. Descripción.",
            dialog_text="9 sugerencias disponibles. Búsquedas recientes.",
            buttons=[_El(text=f"Nav item {i}", role="button") for i in range(72)],
            dismiss_on_escape=True,
        )
        out = await ja._open_easy_apply(_FakeExtractor(page), "123")
        assert out["ok"] is False
        assert out["reason"] == "apply button not clickable"
        assert "reused_open_modal" not in out
        diag = out["diagnostics"]
        assert diag["TOOL_BUILD"] == ja.TOOL_BUILD
        assert diag["main_button_count"] == 72
        assert isinstance(diag["candidates"], list)
        assert len(diag["candidates"]) <= 100
        assert len(diag["candidates"]) == 72
        assert "open_dialog_text" in diag
        assert "Escape" in page.keyboard.presses
        assert all(not b.clicked for b in page._main_buttons)

    async def test_reuses_open_form_modal(self) -> None:
        # Case 28: dialog[open] already holds a form -> reuse, no click.
        page = _FakePage(
            body="Ingeniero. Solicitud sencilla.",
            dialog_text=(
                "Solicitud de empleo. ¿Cuántos años de experiencia tienes? "
                "Adjunta tu curriculum. Teléfono."
            ),
        )
        btn = _apply_button(page)
        page._main_buttons.append(btn)
        out = await ja._open_easy_apply(_FakeExtractor(page), "123")
        assert out["ok"] is True
        assert out["reused_open_modal"] is True
        assert btn.clicked is False
        assert page.apply_clicks == []

    async def test_already_applied(self) -> None:
        # Case 29.
        page = _FakePage(body="Ya has solicitado este empleo.")
        out = await ja._open_easy_apply(_FakeExtractor(page), "123")
        assert out["ok"] is False
        assert out["reason"] == "already applied to this job"

    async def test_non_easy_apply_posting(self) -> None:
        # Case 30.
        page = _FakePage(
            body="Ingeniero de software. Postular en el sitio web de la empresa."
        )
        out = await ja._open_easy_apply(_FakeExtractor(page), "123")
        assert out["ok"] is False
        assert "Easy Apply" in out["reason"]

    async def test_applied_regex_ignores_applicant_stats(self) -> None:
        # Live regression (job 4467280735): the applicant-insights stats
        # ("N personas han solicitado este empleo", third-person plural)
        # must NOT abort as already-applied; the old bare `solicitado`
        # token substring-matched it.
        page = _FakePage(
            body=(
                "Ingeniero de software. Solicitud sencilla. "
                "El 100 % Sin experiencia de personas con nivel "
                "Sin experiencia han solicitado este empleo. "
                "37 solicitados."
            ),
            buttons=[_El(text="Nav item", role="button")],
        )
        out = await ja._open_easy_apply(_FakeExtractor(page), "123")
        assert out.get("reason") != "already applied to this job"

    async def test_spanish_easy_apply_button_discovered(self) -> None:
        # Live regression (job 4467280735): the ES Easy Apply button reads
        # "Solicitud sencilla" as both text and aria-label; the old
        # `solicitar` token never matched it.
        page = _FakePage(body="Ingeniero de software. Solicitud sencilla.")
        btn = _apply_button(page, text="Solicitud sencilla", aria="Solicitud sencilla")
        page._main_buttons.append(btn)
        out = await ja._open_easy_apply(_FakeExtractor(page), "123")
        assert out == {"ok": True}
        assert btn.clicked is True


class TestAppliedRegex:
    def test_ignores_third_person_stats_es(self) -> None:
        assert (
            ja._APPLIED_RE.search("Sin experiencia han solicitado este empleo") is None
        )

    def test_ignores_third_person_stats_en(self) -> None:
        assert (
            ja._APPLIED_RE.search("You're among the 37 applicants who applied") is None
        )

    def test_second_person_still_matches(self) -> None:
        assert ja._APPLIED_RE.search("Ya has solicitado este empleo.") is not None

    def test_helper_verb_banner_sent(self) -> None:
        # Real EN banner inserts a helper verb the contiguous form misses.
        assert ja._APPLIED_RE.search("Your application has been sent") is not None

    def test_helper_verb_banner_submitted(self) -> None:
        assert ja._APPLIED_RE.search("Your application was submitted") is not None

    def test_already_applied_for_this_job(self) -> None:
        assert ja._APPLIED_RE.search("You've already applied for this job") is not None

    def test_ignores_applicant_stats_en_negative(self) -> None:
        # Honest negative (verified by direct regex run): "have applied" and
        # "applied for" appear, but none of the anchored phrases do.
        assert ja._APPLIED_RE.search("applicants have applied for this job") is None


class TestSentConfirmationRegex:
    def test_helper_verb_sent(self) -> None:
        assert (
            ja._SENT_CONFIRMATION_RE.search("Your application has been sent")
            is not None
        )

    def test_solicitud_enviada(self) -> None:
        assert ja._SENT_CONFIRMATION_RE.search("Solicitud enviada") is not None

    def test_se_ha_enviado(self) -> None:
        assert ja._SENT_CONFIRMATION_RE.search("se ha enviado tu solicitud") is not None

    def test_ignores_applicant_stats(self) -> None:
        # Honest negative (verified by direct regex run): applicant-count
        # text carries "applied" but no confirmation phrase.
        assert ja._SENT_CONFIRMATION_RE.search("37 applicants applied") is None


class TestLogApply:
    def test_appends_json_line_with_utc_ts(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        # Case 31: creates parent dirs, one JSON line, UTC ISO ts.
        log = tmp_path / "nested" / "dir" / "apply-log.jsonl"
        monkeypatch.setattr(ja, "APPLY_LOG", str(log))
        ja._log_apply({"tool": "probe", "status": "ok"})
        lines = log.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["tool"] == "probe"
        assert datetime.fromisoformat(entry["ts"]).tzinfo is not None

    async def test_log_contains_no_question_labels(
        self, mock_context: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        # Case 32: the no-page-content-in-logs contract.
        prof = tmp_path / "apply-profile.yml"
        prof.write_text("phone: '+56912345678'\ndefault_years: 3\n", encoding="utf-8")
        monkeypatch.setattr(ja, "PROFILE_PATH", str(prof))
        monkeypatch.setattr(ja, "APPLY_LOG", str(tmp_path / "apply-log.jsonl"))
        page = _FakePage(
            body="Ingeniero. Solicitud sencilla.",
            questions=[_q("Teléfono *"), _q("¿Cuántos años? *")],
        )
        page._main_buttons.append(_apply_button(page))
        tool_fn = await get_tool_fn(_fresh_mcp(), "prepare_application")
        await tool_fn("123", mock_context, extractor=_FakeExtractor(page))
        content = (tmp_path / "apply-log.jsonl").read_text(encoding="utf-8")
        assert "Teléfono" not in content


class TestRegistration:
    async def test_both_tools_registered(self) -> None:
        # Case 33.
        names = [t.name for t in await _fresh_mcp().list_tools()]
        assert "prepare_application" in names
        assert "submit_application" in names

    async def test_extractor_excluded_from_schema(self) -> None:
        # Case 34.
        for name in ("prepare_application", "submit_application"):
            tool = await _fresh_mcp().get_tool(name)
            assert tool is not None
            props = (tool.parameters or {}).get("properties", {})
            assert "extractor" not in props

    async def test_annotations(self) -> None:
        # Case 35.
        prepare = await _fresh_mcp().get_tool("prepare_application")
        submit = await _fresh_mcp().get_tool("submit_application")
        assert prepare is not None and submit is not None

        def _hint(tool: Any, key: str) -> Any:
            ann = tool.annotations
            if isinstance(ann, dict):
                return ann.get(key)
            return getattr(ann, key, None)

        assert _hint(prepare, "readOnlyHint") is True
        assert _hint(submit, "destructiveHint") is True


class TestToolBuild:
    def test_format(self) -> None:
        # Case 36: the deploy marker the operator greps.
        assert isinstance(ja.TOOL_BUILD, str) and ja.TOOL_BUILD
        assert re.match(r"^\d{4}-\d{2}-\d{2}\.\d+$", ja.TOOL_BUILD)
