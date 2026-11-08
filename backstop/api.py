"""The HTTP surface the operations console talks to.

Thin on purpose. Every rule that matters lives in `core.refund` and
`reconcile.reconcile`; this module only moves JSON across the wire. Two
decisions are worth stating, because both are easy to get wrong here:

  * A refusal is a 200.  `Blocked`, `LineAlreadyRefunded`, `PayPalUnknown`
    are the product working, not the server failing. They come back as
    `{"ok": false, "error_type": ...}` with an HTTP 200, so the console can
    render them as what they are instead of a red toast.

  * A fresh `Db` per request.  SQLite connections are not safe to share
    across threads and FastAPI runs sync endpoints in a worker pool, so a
    module-level connection would be a race waiting for the demo.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import provider as provider_mod
from . import seed as seed_mod
from .core import refund as core_refund
from .db import Db
from .reconcile import reconcile as run_reconcile

STATIC_DIR = Path(__file__).resolve().parent / "static"
INDEX_HTML = STATIC_DIR / "index.html"

PLACEHOLDER = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Backstop</title>
<style>
  body { margin:0; min-height:100vh; display:flex; align-items:center;
         justify-content:center; background:#0b0d12; color:#e7e9ee;
         font:16px/1.6 ui-sans-serif, system-ui, -apple-system, sans-serif; }
  main { max-width:34rem; padding:2rem; }
  h1 { font-size:1.5rem; margin:0 0 .5rem; }
  code { background:#161a22; padding:.1rem .35rem; border-radius:4px; }
  p { color:#9aa3b2; }
</style></head>
<body><main>
  <h1>Backstop</h1>
  <p>The console is not built yet. The API is up:
     <code>/api/state</code>, <code>/api/refund</code>,
     <code>/api/reconcile</code>, <code>/api/chaos</code>,
     <code>/api/seed</code>, <code>/api/agent</code>.</p>
  <p>Drop a page at <code>backstop/static/index.html</code> and reload.</p>
</main></body></html>
"""

app = FastAPI(title="Backstop")

# The console is opened straight off disk during development, which makes its
# origin `null`. Permissive CORS, no credentials (a wildcard origin and
# credentials are mutually exclusive in every browser).
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Chaos is held here as well as on the provider, because the provider is a
# singleton that `/api/seed` throws away and rebuilds.
app.state.chaos = (os.getenv("BACKSTOP_CHAOS") or "").lower()


# ---------------------------------------------------------------- plumbing


def _db() -> Db:
    return Db(os.getenv("BACKSTOP_DB") or "backstop.db")


def _pp():
    """The provider, with the chaos switch the console last asked for."""
    pp = provider_mod.get_provider()
    pp.chaos = app.state.chaos
    return pp


def _provider_name(pp) -> str:
    return getattr(pp, "name", None) or type(pp).__name__.lower()


def _message(e: BaseException) -> str:
    return str(e) or type(e).__name__


def _line_json(line, claimed: set[str]) -> dict[str, Any]:
    return {
        "id": line.id,
        "sku": line.sku,
        "description": line.description,
        "amount_minor": line.amount_minor,
        "claimed": line.id in claimed,
    }


def _attempt_json(a) -> dict[str, Any]:
    return {
        "id": a.id,
        "capture_id": a.capture_id,
        "amount_minor": a.amount_minor,
        "currency": a.currency,
        "line_ids": list(a.line_ids or []),
        "state": a.state,
        "paypal_refund_id": a.paypal_refund_id,
        "reason": a.reason or "",
        "error": a.error,
        "created_at": a.created_at,
        "settled_at": a.settled_at,
    }


def _capture_json(db: Db, pp, capture) -> dict[str, Any]:
    claimed = db.claimed_lines(capture.id)

    # Rule 3 on the read side: the balance is PayPal's answer, not our table.
    # If PayPal cannot answer, say so in that one field rather than failing
    # the whole page; a console that will not load is a console that cannot
    # show you the attempt you are worried about.
    refundable: int | None
    balance_error: str | None
    try:
        refundable = pp.refundable_balance_minor(
            order_id=capture.order_id, capture_id=capture.id, currency=capture.currency
        )
        balance_error = None
    except Exception as e:  # noqa: BLE001 - any failure is "we do not know"
        refundable, balance_error = None, _message(e)

    return {
        "id": capture.id,
        "order_id": capture.order_id,
        "currency": capture.currency,
        "amount_minor": capture.amount_minor,
        "buyer_email": capture.buyer_email,
        "created_at": capture.created_at,
        "refundable_minor": refundable,
        "balance_error": balance_error,
        "lines": [_line_json(line, claimed) for line in db.lines(capture.id)],
    }


# ---------------------------------------------------------------- schemas


class RefundIn(BaseModel):
    capture_id: str
    line_ids: list[str] | None = None
    amount_minor: int | None = None
    reason: str = ""


class ChaosIn(BaseModel):
    mode: str | None = ""


class AgentIn(BaseModel):
    capture_id: str
    message: str


# ---------------------------------------------------------------- the API


@app.get("/api/state")
def state() -> dict[str, Any]:
    db, pp = _db(), _pp()
    try:
        return {
            "provider": _provider_name(pp),
            "chaos": app.state.chaos,
            "captures": [_capture_json(db, pp, c) for c in db.captures()],
            "attempts": [_attempt_json(a) for a in db.attempts()],
        }
    finally:
        db.close()


@app.post("/api/refund")
def post_refund(body: RefundIn) -> dict[str, Any]:
    """Always 200. The interesting answers are the refusals."""
    db, pp = _db(), _pp()
    try:
        result = core_refund(
            db,
            pp,
            capture_id=body.capture_id,
            amount_minor=body.amount_minor,
            line_ids=body.line_ids,
            reason=body.reason or "",
        )
    except Exception as e:  # noqa: BLE001 - every bucket renders the same way
        return {
            "ok": False,
            "error_type": type(e).__name__,
            "message": _message(e),
            # PayPalUnknown carries the attempt that is now unresolved. The
            # console needs it to point at the row the reconciler will settle.
            "attempt_id": getattr(e, "attempt_id", None),
        }
    finally:
        db.close()

    return {
        "ok": True,
        "attempt_id": result.attempt_id,
        "state": result.state,
        "paypal_refund_id": result.paypal_refund_id,
    }


@app.post("/api/reconcile")
def post_reconcile(stale_pending_seconds: int = 30) -> dict[str, Any]:
    db, pp = _db(), _pp()
    try:
        return run_reconcile(db, pp, stale_pending_seconds=stale_pending_seconds).as_dict()
    finally:
        db.close()


@app.post("/api/chaos")
def post_chaos(body: ChaosIn) -> dict[str, str]:
    mode = (body.mode or "").strip().lower()
    app.state.chaos = mode
    _pp()  # pushes it onto the live provider instance
    return {"chaos": mode}


@app.post("/api/seed")
def post_seed() -> dict[str, Any]:
    db = _db()
    try:
        seed_mod.seed(db, reset=True)
    finally:
        db.close()
    # The fake provider holds its refunds in memory, so demo data without a
    # fresh provider would leave a capture whose balance is already spent.
    provider_mod.reset_provider()
    _pp()
    return {"ok": True}


@app.post("/api/agent")
def post_agent(body: AgentIn) -> dict[str, Any]:
    db, pp = _db(), _pp()
    try:
        from .agent import run_agent  # lazy: the API boots without the agent

        out = run_agent(db, pp, capture_id=body.capture_id, message=body.message)

        # What the agent carried into the decision, surfaced so the console can
        # show WHY two identical messages got different answers. Read after the
        # run, so it includes anything the agent chose to remember.
        capture = db.capture(body.capture_id)
        buyer = getattr(capture, "buyer_email", None) if capture else None
        facts = db.recall(subject=buyer) if buyer else db.recall(subject=None)
        # Decisions are filed under the capture, not the buyer, so read both or
        # the panel silently omits what the agent just recorded.
        facts += [m for m in db.recall(scope="decision", subject=body.capture_id)
                  if all(m.id != f.id for f in facts)]
        out["memory"] = [
            {"label": m.scope, "value": m.fact, "source": m.source or "agent"}
            for m in facts
        ]
        prior = db.refunds_for_buyer(buyer) if buyer else []
        policy = db.policy()
        out["policy"] = [
            {"label": "Autonomous limit",
             "value": _money(int(policy["autonomous_limit_minor"]), "USD"),
             "source": "policy"},
            {"label": "Refunds allowed per 90 days",
             "value": policy["max_refunds_per_customer_90d"], "source": "policy"},
            {"label": "This buyer, last 90 days",
             "value": f"{len(prior)} refund(s)", "source": "ledger"},
        ]
        return out
    except Exception as e:  # noqa: BLE001 - the console renders one shape
        return {
            "mode": "error",
            "steps": [],
            # Same keys as every normal path, so the console never has to
            # special-case a crash.
            "result": {
                "ok": False,
                "attempt_id": getattr(e, "attempt_id", None),
                "state": None,
                "escalated": False,
                "error_type": type(e).__name__,
                "message": _message(e),
            },
        }
    finally:
        db.close()


def _money(minor: int, currency: str) -> str:
    from .money import to_provider
    return f"{currency} {to_provider(minor, currency)}"


# ---------------------------------------------------------------- console


@app.get("/", response_class=HTMLResponse)
def console() -> Response:
    if INDEX_HTML.is_file():
        return FileResponse(INDEX_HTML, media_type="text/html")
    return HTMLResponse(PLACEHOLDER)


if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
else:
    # The directory may be written after this module is imported (another
    # worker owns the console). Serve it lazily rather than making /static a
    # permanent 404 that nobody thinks to restart for.
    @app.get("/static/{path:path}")
    def static_file(path: str) -> Response:
        target = (STATIC_DIR / path).resolve()
        if STATIC_DIR.is_dir() and target.is_file() and STATIC_DIR.resolve() in target.parents:
            return FileResponse(target)
        return Response(status_code=404)
