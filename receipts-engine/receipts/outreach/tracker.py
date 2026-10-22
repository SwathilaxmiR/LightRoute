"""Seed-test tracker: GET /r/{token} (click log + 302) and POST /form (test contact form).

No IP addresses are stored; the user agent is kept only as a short hash.
"""
from __future__ import annotations

import hashlib
from urllib.parse import parse_qs

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from receipts.db import init_db
from receipts.util.dates import utcnow


def create_app(settings, db_path: str | None = None, mailer=None) -> FastAPI:
    from receipts.outreach.seedtest import SEED_DB, send_form_ack

    app = FastAPI(title="Receipts seed-test tracker")
    path = db_path or str(settings.out_dir / SEED_DB)

    def db():
        return init_db(path)

    @app.get("/r/{token}")
    def redirect(token: str, request: Request):
        d = db()
        ua = request.headers.get("user-agent", "")
        d.insert("clicks", {"token": token, "at": utcnow().isoformat(),
                            "ua_hash": hashlib.sha256(ua.encode()).hexdigest()[:16]})
        d.close()
        return RedirectResponse(settings.seed_test.redirect_target, status_code=302)

    @app.get("/form", response_class=HTMLResponse)
    def form_page():
        return ("<!doctype html><title>Seed-test contact form</title><form method='post' action='/form'>"
                "<input name='lead_id'><input name='token'><textarea name='message'></textarea>"
                "<button>Send</button></form>")

    @app.post("/form")
    async def form(request: Request):
        fields = {k: v[0] for k, v in parse_qs((await request.body()).decode("utf-8", "replace")).items()}
        d = db()
        d.insert("form_submissions", {"lead_id": fields.get("lead_id", ""), "token": fields.get("token", ""),
                                      "message": fields.get("message", "")[:5000], "at": utcnow().isoformat()})
        sent = False
        if fields.get("token"):
            # the "creator" acknowledges by email so reply logging can be exercised end to end (G2 applies)
            sent = send_form_ack(settings, fields["token"], fields.get("lead_id", ""), mailer=mailer)
        d.close()
        return {"stored": True, "ack_emailed": sent}

    return app
