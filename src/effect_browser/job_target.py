from __future__ import annotations

import asyncio
from collections.abc import Callable
from html import escape

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field, field_validator

from effect_browser.store import DatabaseStore

JOB_SLUG = "platform-reliability-engineer"


class JobApplicationBody(BaseModel):
    reference: str = Field(min_length=6, max_length=100)
    job_slug: str
    full_name: str = Field(min_length=2, max_length=200)
    email: str = Field(min_length=5, max_length=320)
    country: str = Field(min_length=2, max_length=100)
    work_authorization: str = Field(min_length=2, max_length=100)
    years_python: int = Field(ge=0, le=50)
    resume_summary: str = Field(min_length=40, max_length=2_000)
    cover_note: str = Field(min_length=20, max_length=2_000)
    mode: str = "real"

    @field_validator("email")
    @classmethod
    def basic_email_shape(cls, value: str) -> str:
        if "@" not in value or "." not in value.rsplit("@", 1)[-1]:
            raise ValueError("email address is invalid")
        return value


def create_demo_job_router(store_provider: Callable[[], DatabaseStore]) -> APIRouter:
    router = APIRouter()

    @router.get("/demo-jobs", response_class=HTMLResponse)
    def jobs() -> str:
        return _shell(
            "Open roles",
            f"""
            <p class="eyebrow">Synthetic ATS &middot; asynchronous UI</p>
            <h1>Careers at Northstar Systems</h1>
            <article class="job-card">
              <div><strong>Platform Reliability Engineer</strong>
              <p>Dublin or remote &middot; Infrastructure</p></div>
              <a href="/demo-jobs/jobs/{JOB_SLUG}/apply">Apply</a>
            </article>
            """,
        )

    @router.get("/demo-jobs/jobs/{job_slug}/apply", response_class=HTMLResponse)
    def application_page(job_slug: str) -> str:
        if job_slug != JOB_SLUG:
            raise HTTPException(404, "job not found")
        content = f"""
        <p class="eyebrow">Application &middot; fields load from the server</p>
        <h1>Platform Reliability Engineer</h1>
        <p class="lede">This form hydrates asynchronously and reveals a conditional
        authorization question after the country changes.</p>
        <div id="form-root" data-testid="dynamic-form-root">
          <div class="loading" role="status">Loading application questions&hellip;</div>
        </div>
        <div id="result" aria-live="polite"></div>
        <script>
        const root = document.querySelector('#form-root');
        const result = document.querySelector('#result');
        const mode = new URLSearchParams(location.search).get('mode') || 'real';
        async function hydrate() {{
          const response = await fetch('/demo-jobs/api/forms/{JOB_SLUG}');
          if (!response.ok) {{ result.textContent = 'Form failed to load'; return; }}
          root.innerHTML = `
            <form id="application-form"
              data-effect-reconciliation-url=
                "/demo-jobs/applications?reference={{effect_key}}"
              data-effect-reconciliation-text=
                "Verified application {{effect_key}}"
              data-effect-receipt-test-id="job-application-receipt">
              <label for="full-name">Full name</label>
              <input id="full-name" name="full_name" required>
              <label for="email">Email</label>
              <input id="email" name="email" type="email" required>
              <label for="country">Country</label>
              <select id="country" name="country" required>
                <option value="">Choose a country</option>
                <option value="Ireland">Ireland</option>
                <option value="United Kingdom">United Kingdom</option>
              </select>
              <div id="authorization-wrap" hidden>
                <label for="authorization">Work authorization</label>
                <select id="authorization" name="work_authorization" required>
                  <option value="">Choose an answer</option>
                  <option value="authorized">
                    Authorized to work in selected country
                  </option>
                  <option value="sponsorship">Requires sponsorship</option>
                </select>
              </div>
              <label for="years-python">Years using Python</label>
              <input id="years-python" name="years_python" type="number"
                min="0" max="50" required>
              <label for="resume-summary">Resume summary</label>
              <textarea id="resume-summary" name="resume_summary" rows="4"
                minlength="40" required></textarea>
              <label for="cover-note">Why this role?</label>
              <textarea id="cover-note" name="cover_note" rows="4"
                minlength="20" required></textarea>
              <label for="reference">Application reference</label>
              <input id="reference" name="reference" required>
              <button type="submit">Submit application</button>
            </form>`;
          const form = document.querySelector('#application-form');
          const country = document.querySelector('#country');
          const authorization = document.querySelector('#authorization-wrap');
          country.addEventListener('change', () => {{
            authorization.hidden = !country.value;
          }});
          form.addEventListener('submit', async (event) => {{
            event.preventDefault();
            result.innerHTML =
              '<div class="loading">Submitting application&hellip;</div>';
            const payload = Object.fromEntries(new FormData(form).entries());
            payload.job_slug = '{JOB_SLUG}';
            payload.years_python = Number(payload.years_python);
            payload.mode = mode;
            const submitted = await fetch('/demo-jobs/api/applications', {{
              method: 'POST',
              headers: {{'Content-Type': 'application/json'}},
              body: JSON.stringify(payload)
            }});
            if (submitted.ok || mode === 'fake_success') {{
              root.hidden = true;
              result.innerHTML = `<div class="success" data-testid="client-success">
                <strong>Application received</strong>
                <span>Reference ${{payload.reference}}</span>
              </div>`;
            }} else {{
              result.innerHTML =
                `<div class="error">Application rejected by server</div>`;
            }}
          }});
        }}
        hydrate();
        </script>
        """
        return _shell("Apply", content)

    @router.get("/demo-jobs/api/forms/{job_slug}")
    async def form_definition(job_slug: str) -> dict:
        if job_slug != JOB_SLUG:
            raise HTTPException(404, "job not found")
        await asyncio.sleep(0.35)
        return {
            "job_slug": JOB_SLUG,
            "version": 3,
            "required": [
                "full_name",
                "email",
                "country",
                "work_authorization",
                "years_python",
                "resume_summary",
                "cover_note",
                "reference",
            ],
        }

    @router.post("/demo-jobs/api/applications")
    def submit_application(body: JobApplicationBody) -> dict:
        if body.job_slug != JOB_SLUG:
            raise HTTPException(422, "job is not open")
        if body.mode == "reject":
            raise HTTPException(422, "synthetic ATS rejected the application")
        if body.mode == "fake_success":
            return {"accepted": True, "persisted": False, "application_id": None}
        application_id, created = store_provider().create_demo_job_application(
            reference=body.reference,
            job_slug=body.job_slug,
            full_name=body.full_name,
            email=body.email,
            country=body.country,
            work_authorization=body.work_authorization,
            years_python=body.years_python,
            resume_summary=body.resume_summary,
            cover_note=body.cover_note,
        )
        return {
            "accepted": True,
            "persisted": True,
            "application_id": application_id,
            "created": created,
        }

    @router.get("/demo-jobs/applications", response_class=HTMLResponse)
    def find_application(reference: str = "") -> str:
        application = (
            store_provider().demo_job_application(reference) if reference else None
        )
        if application is None:
            content = """
            <p class="eyebrow">Authoritative ATS ledger</p>
            <h1>No verified application</h1>
            <p class="lede">The browser may have shown success, but no durable
            application exists for this reference.</p>
            """
        else:
            application_id = escape(application["id"])
            safe_reference = escape(application["reference"])
            content = f"""
            <p class="eyebrow">Authoritative ATS ledger</p>
            <h1>Verified application</h1>
            <div class="receipt" data-testid="job-application-receipt"
              data-external-id="{application_id}">
              <strong>Verified application {safe_reference}</strong>
              <span>Application ID {application_id}</span>
              <span>{escape(application["full_name"])}</span>
              <span>Duplicate attempts: {application["duplicate_attempts"]}</span>
            </div>
            """
        return _shell("Application receipt", content)

    @router.get("/demo-jobs/api/applications")
    def application_ledger() -> list[dict]:
        return store_provider().demo_job_applications()

    return router


def _shell(title: str, content: str) -> str:
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport"
content="width=device-width,initial-scale=1"><title>{escape(title)} - Northstar</title>
<style>
:root{{--ink:#18211f;--muted:#66716e;--paper:#fbfaf6;--accent:#c8583d}}
*{{box-sizing:border-box}} body{{max-width:820px;margin:0 auto;padding:64px 28px;
font-family:system-ui,sans-serif;color:var(--ink);background:#edf0eb}}
h1{{font:500 48px Georgia,serif;margin:8px 0 16px;letter-spacing:-.04em}}
.eyebrow{{font-size:11px;text-transform:uppercase;letter-spacing:.16em;color:var(--muted)}}
.lede{{max-width:650px;color:var(--muted);line-height:1.6}} form,.receipt,.job-card,
.success,.error{{display:grid;gap:11px;margin-top:28px;padding:28px;
border:1px solid #d1d7d1;
border-radius:10px;background:var(--paper);box-shadow:0 16px 45px #18211f12}}
label{{margin-top:5px;font-size:12px;font-weight:750}}
input,select,textarea{{padding:13px;border:1px solid #b7c2ba;border-radius:6px;
background:white;font:inherit}} button,a{{padding:14px;
border:0;border-radius:6px;color:white;background:var(--ink);font-weight:750;
text-decoration:none;text-align:center;cursor:pointer}}
.job-card{{grid-template-columns:1fr auto;
align-items:center}} .job-card p,.loading,.receipt span{{color:var(--muted)}}
.success{{border-left:5px solid #388365}} .error{{border-left:5px solid var(--accent)}}
</style></head><body>{content}</body></html>"""
