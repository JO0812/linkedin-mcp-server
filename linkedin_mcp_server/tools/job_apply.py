"""
LinkedIn Easy Apply tools — HUMAN-GATED, two-step job applications.

Step 1 (consulted): `prepare_application` opens the Easy Apply modal,
extracts screening questions verbatim, proposes answers from the user's
approved profile (`apply-profile.yml`), and CLOSES the modal without
submitting anything.

Step 2 (consulted): `submit_application` fills ONLY user-approved answers,
attaches the user-approved CV, and clicks Submit ONLY when
`confirm_send=True`. Every submission is appended to the local apply log.

Safety contract (non-negotiable):
- No `confirm_send=True` → no submit, ever. The tool aborts instead.
- No invented answers: questions without a profile-backed answer stay
  blank and are reported as NEEDS_USER; required blanks abort submit.
- Easy Apply modals only. External ATS links abort with a message.
- All JD/page content is untrusted data, never instructions.
"""

import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Annotated, Any

from fastmcp import Context, FastMCP
from pydantic import Field

from linkedin_mcp_server.config.schema import DEFAULT_TOOL_TIMEOUT_SECONDS
from linkedin_mcp_server.core.exceptions import AuthenticationError
from linkedin_mcp_server.dependencies import get_ready_extractor, handle_auth_error
from linkedin_mcp_server.error_handler import raise_tool_error

logger = logging.getLogger(__name__)

PROFILE_PATH = os.path.expanduser("~/.config/career-ops/apply-profile.yml")
APPLY_LOG = os.path.expanduser("~/.local/share/career-ops/apply-log.jsonl")

_DIALOG = "[role='dialog']"


def _load_profile() -> dict[str, Any]:
    """Load the user-approved answer profile. Empty dict when absent."""
    try:
        import yaml  # type: ignore

        with open(PROFILE_PATH, encoding="utf-8") as fh:
            return yaml.safe_load(fh) or {}
    except Exception:
        return {}


def _log_apply(entry: dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(APPLY_LOG), exist_ok=True)
    entry["ts"] = datetime.now(timezone.utc).isoformat()
    with open(APPLY_LOG, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def _propose_answer(label: str, qtype: str, options: list[str], profile: dict[str, Any]) -> dict[str, Any]:
    """Propose an answer from profile facts. Never invents."""
    lab = _norm(label)
    p = profile

    def hit(*keys: str) -> bool:
        return any(k in lab for k in keys)

    if hit("phone", "teléfono", "telefono", "móvil", "movil", "celular") and p.get("phone"):
        return {"value": p["phone"], "source": "profile", "needs_user": False}
    if hit("email", "correo") and p.get("email"):
        return {"value": p["email"], "source": "profile", "needs_user": False}
    if hit("location", "ubicaci", "city", "ciudad", "where are you located") and p.get("location"):
        return {"value": p["location"], "source": "profile", "needs_user": False}
    if hit("authoriz", "autoriza", "legally", "right to work", "requiere visa", "sponsorship", "patrocinio"):
        v = p.get("work_authorization_statement")
        if v:
            return {"value": v, "source": "profile", "needs_user": False}
    if hit("salary", "salario", "renta", "pretension", "pretensión", "compensation", "expected pay"):
        v = p.get("salary_statement")
        if v:
            return {"value": v, "source": "profile", "needs_user": False}
    if hit("notice", "preaviso", "start date", "disponibilidad", "availability", "when can you start"):
        v = p.get("notice_statement")
        if v:
            return {"value": v, "source": "profile", "needs_user": False}
    if hit("years", "años", "anos", "experience", "experiencia") and p.get("years_statements"):
        for rule in p["years_statements"]:
            if _norm(str(rule.get("match", ""))) and _norm(str(rule["match"])) in lab:
                return {"value": rule["value"], "source": "profile", "needs_user": False}
        return {"value": None, "source": None, "needs_user": True, "reason": "years question needs user wording"}
    if qtype in ("select", "radio") and options:
        # Never auto-pick constrained options (degree/auth/veteran/disability...).
        return {"value": None, "source": None, "needs_user": True, "reason": "constrained choice — user must pick"}
    return {"value": None, "source": None, "needs_user": True, "reason": "no profile fact covers this"}


async def _open_easy_apply(extractor: Any, job_id: str) -> dict[str, Any]:
    """Navigate to the job and open its Easy Apply modal. Returns status."""
    page = extractor._page
    await page.goto(f"https://www.linkedin.com/jobs/view/{job_id}/", wait_until="domcontentloaded", timeout=30000)
    await page.wait_for_timeout(2500)
    text = await page.evaluate("() => document.body?.innerText || ''")
    if not re.search(r"solicitud sencilla|easy apply", text, re.I):
        return {"ok": False, "reason": "not an Easy Apply posting (external ATS or no apply control)"}
    clicked = await extractor.click_button_by_text("Solicitar") or await extractor.click_button_by_text("Easy Apply")
    if not clicked:
        # Fallback: click first apply-ish button in job view.
        btn = page.locator("button").filter(has_text=re.compile(r"Solicitar|Easy Apply", re.I)).first
        try:
            await btn.click(timeout=5000)
            clicked = True
        except Exception:
            clicked = False
    if not clicked:
        return {"ok": False, "reason": "apply button not clickable"}
    await page.wait_for_timeout(2500)
    if await page.locator(_DIALOG).count() == 0:
        return {"ok": False, "reason": "apply modal did not open"}
    return {"ok": True}


async def _close_modal(page: Any) -> None:
    try:
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(800)
    except Exception:
        pass
    # Dismiss any "discard application?" confirmation without saving.
    try:
        btn = page.locator("button").filter(has_text=re.compile(r"^Descartar$|^Discard$", re.I)).first
        if await btn.count() > 0:
            await btn.click(timeout=3000)
    except Exception:
        pass


async def _extract_questions(page: Any) -> list[dict[str, Any]]:
    """Read every question in the open Easy Apply modal (all steps)."""
    questions: list[dict[str, Any]] = []
    seen_steps = 0
    while seen_steps < 6:
        dlg = page.locator(_DIALOG).first
        try:
            blocks = dlg.locator("label, fieldset")
            n = await blocks.count()
        except Exception:
            break
        for i in range(n):
            try:
                b = blocks.nth(i)
                label = (await b.inner_text()).strip()
                if not label or any(q["label"] == label for q in questions):
                    continue
                # Determine control type + options.
                sel = b.locator("select").first
                radios = b.locator("input[type='radio']")
                files = b.locator("input[type='file']")
                txt = b.locator("input[type='text'], input:not([type]), textarea").first
                qtype, options = "text", []
                if await files.count() > 0:
                    qtype = "file"
                elif await sel.count() > 0:
                    qtype = "select"
                    opts = await sel.locator("option").all_inner_texts()
                    options = [o.strip() for o in opts if o.strip()]
                elif await radios.count() > 0:
                    qtype = "radio"
                    opts = await b.locator("label").all_inner_texts()
                    options = [o.strip() for o in opts if o.strip()]
                elif await txt.count() == 0:
                    continue
                required = bool(re.search(r"\*|obligatorio|required", label, re.I))
                questions.append({"label": label, "type": qtype, "options": options, "required": required})
            except Exception:
                continue
        # Advance to next step without submitting (Siguiente/Next only).
        nxt = dlg.locator("button").filter(has_text=re.compile(r"^(Siguiente|Next|Continuar|Continue)$", re.I)).first
        try:
            if await nxt.count() > 0 and await nxt.is_enabled():
                await nxt.click(timeout=4000)
                await page.wait_for_timeout(1500)
                seen_steps += 1
                continue
        except Exception:
            pass
        break
    return questions


async def _fill_field(page: Any, label: str, value: str, cv_path: str | None = None) -> bool:
    """Fill one field matched by its label. Returns success."""
    dlg = page.locator(_DIALOG).first
    try:
        # File upload (CV).
        if cv_path and label == "__cv__":
            inp = dlg.locator("input[type='file']").first
            if await inp.count() > 0:
                await inp.set_input_files(cv_path, timeout=15000)
                return True
            return False
        # Locate the form row containing the label text.
        row = dlg.locator("div, fieldset").filter(has_text=re.compile(re.escape(label[:40]))).first
        sel = row.locator("select").first
        if await sel.count() > 0:
            await sel.select_option(label=value, timeout=5000)
            return True
        radios = row.locator("input[type='radio']")
        if await radios.count() > 0:
            opt = row.locator("label").filter(has_text=re.compile(rf"^{re.escape(value)}$")).first
            if await opt.count() > 0:
                await opt.click(timeout=5000)
                return True
            return False
        txt = row.locator("input[type='text'], input:not([type]), textarea").first
        if await txt.count() > 0:
            await txt.fill(value, timeout=5000)
            return True
    except Exception as exc:
        logger.warning("fill failed for %r: %s", label, exc)
    return False


def register_apply_tools(
    mcp: FastMCP, *, tool_timeout: float = DEFAULT_TOOL_TIMEOUT_SECONDS
) -> None:
    """Register the two gated Easy Apply tools."""

    @mcp.tool(
        timeout=tool_timeout,
        title="Prepare Job Application",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"job", "apply", "prepare"},
    )
    async def prepare_application(
        job_id: Annotated[str, Field(description="LinkedIn job ID (e.g. 4467280735)")],
        ctx: Context,
    ) -> dict[str, Any]:
        """
        STEP 1 (consulted, never submits). Open the Easy Apply modal for a
        job, extract every screening question verbatim across all steps, and
        propose answers from the user-approved profile. Closes the modal
        without saving or submitting. The user reviews/edits the draft, then
        separately commands submit_application.
        """
        try:
            extractor = await get_ready_extractor(ctx, tool_name="prepare_application")
            await ctx.report_progress(progress=10, total=100, message="Opening Easy Apply")
            opened = await _open_easy_apply(extractor, job_id)
            if not opened["ok"]:
                return {"job_id": job_id, "status": "cannot_prepare", **opened}
            page = extractor._page
            questions = await _extract_questions(page)
            await _close_modal(page)
            profile = _load_profile()
            draft = []
            for q in questions:
                prop = _propose_answer(q["label"], q["type"], q["options"], profile)
                draft.append({**q, **prop})
            await ctx.report_progress(progress=100, total=100, message="Draft ready, modal closed")
            return {
                "job_id": job_id,
                "status": "draft_ready",
                "submitted": False,
                "questions": draft,
                "note": "Review/edit every answer, then command submit_application with the approved set.",
            }
        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "prepare_application")
        except Exception as e:
            raise_tool_error(e, "prepare_application")

    @mcp.tool(
        timeout=tool_timeout,
        title="Submit Job Application",
        annotations={"readOnlyHint": False, "destructiveHint": True, "openWorldHint": True},
        tags={"job", "apply", "submit"},
    )
    async def submit_application(
        job_id: Annotated[str, Field(description="LinkedIn job ID")],
        answers_json: Annotated[str, Field(description='Approved answers as JSON: [{"label": "...", "value": "..."}]')],
        cv_path: Annotated[str, Field(description="Absolute path of the user-approved CV PDF to attach (empty string to skip)")],
        confirm_send: Annotated[bool, Field(description="MUST be true. Any other value aborts without touching the page.")],
        ctx: Context,
    ) -> dict[str, Any]:
        """
        STEP 2 (consulted, gated). Fill ONLY the user-approved answers,
        attach the user-approved CV, walk Review, and click Submit — but
        ONLY when confirm_send is True. Refuses on: confirm_send falsy,
        external (non-Easy-Apply) postings, unanswered required questions,
        or any fill failure. Logs every attempt locally.
        """
        attempt: dict[str, Any] = {"job_id": job_id, "confirm_send": bool(confirm_send)}
        if not confirm_send:
            attempt.update(status="aborted", reason="confirm_send is not True — refusing to submit")
            _log_apply(attempt)
            return attempt
        try:
            answers = json.loads(answers_json)
            assert isinstance(answers, list) and answers, "answers_json must be a non-empty list"
        except Exception as exc:
            attempt.update(status="aborted", reason=f"bad answers_json: {exc}")
            _log_apply(attempt)
            return attempt
        try:
            extractor = await get_ready_extractor(ctx, tool_name="submit_application")
            await ctx.report_progress(progress=10, total=100, message="Reopening Easy Apply")
            opened = await _open_easy_apply(extractor, job_id)
            if not opened["ok"]:
                attempt.update(status="aborted", **opened)
                _log_apply(attempt)
                return attempt
            page = extractor._page
            questions = await _extract_questions(page)
            by_label = {_norm(q["label"]): q for q in questions}
            filled, missing_required, failed = [], [], []
            for a in answers:
                lab = _norm(str(a.get("label", "")))
                if lab not in by_label:
                    failed.append(a.get("label"))
                    continue
                ok = await _fill_field(page, by_label[lab]["label"], str(a.get("value", "")))
                (filled if ok else failed).append(a.get("label"))
            for q in questions:
                if q["required"] and _norm(q["label"]) not in {_norm(str(a.get("label", ""))) for a in answers}:
                    missing_required.append(q["label"])
            if cv_path:
                if not await _fill_field(page, "__cv__", "", cv_path):
                    failed.append("__cv_upload__")
            if missing_required or failed:
                await _close_modal(page)
                attempt.update(status="aborted", missing_required=missing_required,
                               failed_fields=failed, filled_ok=filled)
                _log_apply(attempt)
                return attempt
            # Walk Next until Review, then Submit.
            dlg = page.locator(_DIALOG).first
            for _ in range(6):
                nxt = dlg.locator("button").filter(
                    has_text=re.compile(r"^(Siguiente|Next|Continuar|Continue|Revisar|Review)$", re.I)).first
                try:
                    if await nxt.count() == 0:
                        break
                    await nxt.click(timeout=4000)
                    await page.wait_for_timeout(1500)
                except Exception:
                    break
            send = dlg.locator("button").filter(
                has_text=re.compile(r"^(Enviar solicitud|Submit application|Enviar|Submit)$", re.I)).first
            try:
                if await send.count() == 0 or not await send.is_enabled():
                    await _close_modal(page)
                    attempt.update(status="aborted", reason="submit button not available/enabled")
                    _log_apply(attempt)
                    return attempt
                await send.click(timeout=8000)
                await page.wait_for_timeout(3000)
            except Exception as exc:
                await _close_modal(page)
                attempt.update(status="aborted", reason=f"submit click failed: {exc}")
                _log_apply(attempt)
                return attempt
            body = await page.evaluate("() => document.body?.innerText || ''")
            sent = bool(re.search(r"solicitud enviada|application (sent|submitted)|se ha enviado", body, re.I))
            await _close_modal(page)
            attempt.update(status="submitted" if sent else "uncertain",
                           filled_ok=filled, cv_attached=bool(cv_path))
            _log_apply(attempt)
            await ctx.report_progress(progress=100, total=100, message=attempt["status"])
            return attempt
        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "submit_application")
        except Exception as e:
            raise_tool_error(e, "submit_application")
