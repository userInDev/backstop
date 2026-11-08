"""The money desk.

One agent across the merchant's PayPal relationship: it reads an order, recalls
what the company already knows about the buyer, checks the policy envelope,
refunds, pays a vendor, or hands the decision to a person. It remembers what it
decided, so the next message from the same customer is not answered from a
blank page.

Two things are true about it at once, and both have to be, or it is not
something you leave running.

It has judgement. It reads a sentence a person wrote and decides what it means.
Two customers can send the same sentence and get different answers, because
memory knows one of them has had four refunds in sixty days.

It has no reach. There is no PayPal client on this module. Every refund goes
through `core.refund`, which runs the four rules; payouts are shut behind
`provider.supports_payouts()` and, when opened, route through core too. The
policy envelope is enforced inside `refund_lines`, from the capture's own
amount and the capture's own buyer, not from anything the agent says. An agent
that talks itself into ignoring the limit is stopped by the same code as an
agent that never heard of it.

Two modes, chosen at runtime by whether ANTHROPIC_API_KEY is set:

    claude      A real tool-use loop.
    scripted    A deterministic matcher, so the demo runs with no credentials.

Both go through the same tools, so the backstop sees no difference between
them, which is the point.
"""

from __future__ import annotations

import json
import os
import re

from . import core, provider
from .errors import BackstopError, PayPalUnknown
from .money import to_provider

MODEL = "claude-sonnet-5-5"
MAX_ITERATIONS = 10
MAX_TOKENS = 16000

MEMORY_SCOPES = ("customer", "vendor", "policy", "decision")
DEFAULT_CURRENCY = "USD"

SYSTEM = """You are the money desk for an online print shop. You read one support
message and settle it: refund something, pay a vendor, or hand it to a person.

How you work:

  Recall before you decide. Call get_order and then recall for the buyer, every
  time, before anything that moves money. The company remembers this customer
  even when the message in front of you does not mention it. Say out loud what
  memory told you.

  Never guess an amount. You refund by line id and the amount comes from the
  order. No tool takes an amount for a refund, deliberately.

  Refund only what the customer says is wrong. If one item arrived damaged and
  the rest is fine, refund that line. If the message names nothing refundable,
  refund nothing and say so.

  Check the policy envelope with check_policy before refunding. If it comes
  back not allowed, do not try anyway and do not reword it. Call escalate with
  the reason and what you would have done. Escalating is a good outcome, not a
  failure.

  A refusal is an answer. If a tool comes back with ok false, read why and
  report it. Do not call it again.

  Record one thing. After the decision, call remember with scope decision and
  the buyer as subject, one sentence, so the next person to see this customer
  knows what happened.

Finish with one or two sentences for the human support agent: what you did, and
what memory and policy had to do with it."""


def _schema(props: dict, required: list[str]) -> dict:
    return {
        "type": "object",
        "properties": props,
        "required": required,
        "additionalProperties": False,
    }


_CAPTURE = {"type": "string", "description": "The PayPal capture id."}

TOOLS = [
    {
        "name": "get_order",
        "description": (
            "Read one capture: its buyer, currency, total, every line item "
            "(id, description, amount in minor units), and every refund attempt "
            "against it so far. Call this first."
        ),
        "strict": True,
        "input_schema": _schema({"capture_id": _CAPTURE}, ["capture_id"]),
    },
    {
        "name": "recall",
        "description": (
            "What the company knows about this buyer: remembered facts, standing "
            "policy notes, and their succeeded refunds in the last 90 days. Call "
            "this before any decision that moves money. The same message from two "
            "customers can deserve two different answers and this is where you "
            "find out which one you are holding."
        ),
        "strict": True,
        "input_schema": _schema(
            {"subject": {"type": "string", "description": "The buyer's email, a vendor id, or 'global'."}},
            ["subject"],
        ),
    },
    {
        "name": "check_policy",
        "description": (
            "Ask whether a refund of this size to this buyer is inside the envelope "
            "the merchant set. Returns allowed plus the reasons. Not advice: the "
            "same check runs inside refund_lines and refuses there too."
        ),
        "strict": True,
        "input_schema": _schema(
            {
                "amount_minor": {"type": "integer", "description": "The refund total in minor units."},
                "buyer_email": {"type": "string", "description": "The buyer, from get_order."},
            },
            ["amount_minor", "buyer_email"],
        ),
    },
    {
        "name": "refund_lines",
        "description": (
            "Refund whole line items. The amount is derived from the order, not from "
            "you. Returns ok true with an attempt id and state, or ok false with an "
            "error_type explaining the refusal. Never call it twice for one complaint."
        ),
        "strict": True,
        "input_schema": _schema(
            {
                "capture_id": _CAPTURE,
                "line_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Line ids from get_order, exactly as written there.",
                },
                "reason": {"type": "string", "description": "One short sentence recorded on the attempt."},
            },
            ["capture_id", "line_ids", "reason"],
        ),
    },
    {
        "name": "pay_vendor",
        "description": (
            "Send money out to a vendor through PayPal Payouts. Currently shut off "
            "at the provider, so it answers with an error. Ask for it when a vendor "
            "genuinely owes work, and expect to escalate instead."
        ),
        "strict": True,
        "input_schema": _schema(
            {
                "vendor_id": {"type": "string", "description": "The vendor's payout id."},
                "amount_minor": {"type": "integer", "description": "Amount in minor units."},
                "reason": {"type": "string", "description": "Why this vendor is owed."},
            },
            ["vendor_id", "amount_minor", "reason"],
        ),
    },
    {
        "name": "escalate",
        "description": (
            "Hand the decision to a person, with what you would have done. Use it "
            "when policy says no, when the message is ambiguous, or when you would "
            "be guessing. This is the honourable exit and it counts as finishing."
        ),
        "strict": True,
        "input_schema": _schema(
            {
                "reason": {"type": "string", "description": "Why a person has to see this."},
                "suggested_action": {"type": "string", "description": "What you would do if it were yours."},
            },
            ["reason", "suggested_action"],
        ),
    },
    {
        "name": "remember",
        "description": (
            "Write one fact into company memory so the next decision has it. Scope is "
            "customer, vendor, policy or decision. Subject is an email, a vendor id, "
            "or 'global'. One sentence."
        ),
        "strict": True,
        "input_schema": _schema(
            {
                "scope": {"type": "string", "enum": list(MEMORY_SCOPES), "description": "customer | vendor | policy | decision"},
                "subject": {"type": "string", "description": "An email, a vendor id, or 'global'."},
                "fact": {"type": "string", "description": "One sentence worth reading in six months."},
            },
            ["scope", "subject", "fact"],
        ),
    },
    {
        "name": "get_refund_status",
        "description": "The refund attempts on this capture: pending, unknown, failed or succeeded.",
        "strict": True,
        "input_schema": _schema({"capture_id": _CAPTURE}, ["capture_id"]),
    },
]


# ---------------------------------------------------------------------------
# Outcomes
# ---------------------------------------------------------------------------


def _outcome(ok, *, attempt_id=None, state=None, error_type=None, message=None, escalated=False) -> dict:
    return {
        "ok": ok,
        "attempt_id": attempt_id,
        "state": state,
        "error_type": error_type,
        "message": message,
        "escalated": escalated,
    }


NO_REFUND = _outcome(False, message="the agent finished without moving any money")


def _attempt(a) -> dict:
    return {
        "id": a.id,
        "state": a.state,
        "amount_minor": a.amount_minor,
        "currency": a.currency,
        "line_ids": a.line_ids,
        "paypal_refund_id": a.paypal_refund_id,
        "reason": a.reason,
        "error": a.error,
        "created_at": a.created_at,
    }


def _fact(m) -> dict:
    return {"id": m.id, "scope": m.scope, "subject": m.subject, "fact": m.fact,
            "source": m.source, "created_at": m.created_at}


def _payouts_enabled() -> bool:
    """One switch, and it lives in provider.py. Absent means no."""
    fn = getattr(provider, "supports_payouts", None)
    return bool(fn()) if callable(fn) else False


# ---------------------------------------------------------------------------
# The tools
# ---------------------------------------------------------------------------


class Tools:
    """Everything the agent can reach.

    There is no provider handle exposed here and no tool that takes an amount
    for a refund. `refund_lines` moves money by calling core.refund and nothing
    else; `pay_vendor` is shut at the provider and would route through core
    too. The policy envelope is checked inside refund_lines, against the
    capture's own amount and the capture's own buyer, so an agent that skips
    check_policy or reports a flattering number changes nothing.
    """

    def __init__(self, db, pp):
        self._db = db
        self._pp = pp
        # The last outcome that settled the run. None means nothing moved.
        self.outcome: dict | None = None

    def call(self, name: str, args: dict) -> dict:
        a = dict(args or {})
        if name == "get_order":
            return self.get_order(a.get("capture_id"))
        if name == "recall":
            return self.recall(a.get("subject"))
        if name == "check_policy":
            return self.check_policy(a.get("amount_minor"), a.get("buyer_email"))
        if name == "refund_lines":
            return self.refund_lines(a.get("capture_id"), a.get("line_ids") or [], a.get("reason") or "")
        if name == "pay_vendor":
            return self.pay_vendor(a.get("vendor_id"), a.get("amount_minor"), a.get("reason") or "")
        if name == "escalate":
            return self.escalate(a.get("reason") or "", a.get("suggested_action") or "")
        if name == "remember":
            return self.remember(a.get("scope"), a.get("subject"), a.get("fact"))
        if name == "get_refund_status":
            return self.get_refund_status(a.get("capture_id"))
        return {"error": f"no such tool: {name}"}

    # -- reading ----------------------------------------------------------

    def get_order(self, capture_id) -> dict:
        capture = self._db.capture(capture_id)
        if capture is None:
            return {"error": f"unknown capture {capture_id}"}
        return {
            "capture_id": capture.id,
            "order_id": capture.order_id,
            "buyer_email": capture.buyer_email,
            "currency": capture.currency,
            "amount_minor": capture.amount_minor,
            "lines": [
                {"id": ln.id, "sku": ln.sku, "description": ln.description, "amount_minor": ln.amount_minor}
                for ln in self._db.lines(capture_id)
            ],
            "attempts": [_attempt(a) for a in self._db.attempts(capture_id)],
        }

    def recall(self, subject) -> dict:
        subject = subject or "global"
        history = self._db.refunds_for_buyer(subject, within_days=90) if subject != "global" else []
        return {
            "subject": subject,
            "policy": self._db.policy(),
            "facts": [_fact(m) for m in self._db.recall(subject=subject)],
            "refunds_90d": {
                "count": len(history),
                "total_minor": sum(a.amount_minor for a in history),
                "attempts": [_attempt(a) for a in history],
            },
        }

    def get_refund_status(self, capture_id) -> dict:
        return {
            "capture_id": capture_id,
            "attempts": [_attempt(a) for a in self._db.attempts(capture_id)],
            "unresolved": [a.id for a in self._db.unresolved(capture_id)],
        }

    # -- the envelope -----------------------------------------------------

    def check_policy(self, amount_minor, buyer_email, *, currency=DEFAULT_CURRENCY) -> dict:
        policy = self._db.policy()

        def _int(key, fallback):
            try:
                return int(policy.get(key, fallback))
            except (TypeError, ValueError):
                return fallback

        limit = _int("autonomous_limit_minor", 5000)
        approval_over = _int("require_approval_over_minor", 5000)
        max_refunds = _int("max_refunds_per_customer_90d", 3)

        try:
            amount = int(amount_minor)
        except (TypeError, ValueError):
            return {"allowed": False, "reasons": [f"amount {amount_minor!r} is not a number"],
                    "amount_minor": amount_minor, "buyer_email": buyer_email}

        history = self._db.refunds_for_buyer(buyer_email, within_days=90) if buyer_email else []
        reasons = []
        if amount > limit:
            reasons.append(
                f"{to_provider(amount, currency)} {currency} is over the autonomous limit of "
                f"{to_provider(limit, currency)} {currency}"
            )
        if amount > approval_over and approval_over != limit:
            reasons.append(
                f"{to_provider(amount, currency)} {currency} needs a person's approval "
                f"(over {to_provider(approval_over, currency)} {currency})"
            )
        if len(history) >= max_refunds:
            reasons.append(
                f"{buyer_email} has had {len(history)} refunds in the last 90 days; "
                f"the limit is {max_refunds}"
            )
        return {
            "allowed": not reasons,
            "reasons": reasons,
            "amount_minor": amount,
            "buyer_email": buyer_email,
            "refunds_90d": len(history),
            "policy": {
                "autonomous_limit_minor": limit,
                "require_approval_over_minor": approval_over,
                "max_refunds_per_customer_90d": max_refunds,
            },
        }

    # -- money ------------------------------------------------------------

    def refund_lines(self, capture_id, line_ids, reason="") -> dict:
        line_ids = [str(i) for i in (line_ids or [])]

        # The envelope is checked here, from the order, before core.refund is
        # called at all. The agent supplies line ids and nothing else: the
        # amount and the buyer both come out of the database, so there is
        # nothing it can say that makes a blocked refund look allowed.
        capture = self._db.capture(capture_id)
        if capture is not None and line_ids:
            by_id = {ln.id: ln for ln in self._db.lines(capture_id)}
            if all(i in by_id for i in line_ids):
                amount = sum(by_id[i].amount_minor for i in line_ids)
                verdict = self.check_policy(amount, capture.buyer_email, currency=capture.currency)
                if not verdict["allowed"]:
                    self.outcome = _outcome(
                        False,
                        error_type="PolicyBlocked",
                        message="policy refuses this refund: " + "; ".join(verdict["reasons"]),
                        escalated=True,
                    )
                    return {**dict(self.outcome), "reasons": verdict["reasons"], "policy": verdict["policy"]}

        try:
            res = core.refund(
                self._db, self._pp, capture_id=capture_id, line_ids=line_ids, reason=reason
            )
        except BackstopError as e:
            # The refusal is the product. Every rule in core.py reaches the
            # caller as an answer, never as a crash.
            self.outcome = _outcome(
                False,
                attempt_id=getattr(e, "attempt_id", None),
                state="unknown" if isinstance(e, PayPalUnknown) else None,
                error_type=type(e).__name__,
                message=str(e),
            )
            return dict(self.outcome)

        self.outcome = _outcome(
            True,
            attempt_id=res.attempt_id,
            state=res.state,
            message=f"refunded {to_provider(res.amount_minor, res.currency)} {res.currency}",
        )
        return dict(self.outcome)

    def pay_vendor(self, vendor_id, amount_minor, reason="") -> dict:
        """Money going OUT.

        Shut at the provider until sandbox Payouts permissions are verified, so
        it is never on the critical path. Opening it is a change in the two
        files that own money, not here: `provider.supports_payouts()` returns
        True and `core.payout` exists to run the rules. This module still holds
        no provider handle either way.
        """
        if not _payouts_enabled():
            return {"error": "payouts not enabled"}

        pay = getattr(core, "payout", None)
        if pay is None:
            return {"error": "payouts not enabled: core has no payout path yet"}

        verdict = self.check_policy(amount_minor, None)
        if not verdict["allowed"]:
            self.outcome = _outcome(
                False,
                error_type="PolicyBlocked",
                message="policy refuses this payout: " + "; ".join(verdict["reasons"]),
                escalated=True,
            )
            return {**dict(self.outcome), "reasons": verdict["reasons"]}

        try:
            res = pay(self._db, self._pp, vendor_id=vendor_id,
                      amount_minor=verdict["amount_minor"], reason=reason)
        except BackstopError as e:
            self.outcome = _outcome(
                False,
                attempt_id=getattr(e, "attempt_id", None),
                state="unknown" if isinstance(e, PayPalUnknown) else None,
                error_type=type(e).__name__,
                message=str(e),
            )
            return dict(self.outcome)

        self.outcome = _outcome(
            True,
            attempt_id=getattr(res, "attempt_id", None),
            state=getattr(res, "state", "succeeded"),
            message=f"paid {vendor_id}",
        )
        return dict(self.outcome)

    # -- the exits --------------------------------------------------------

    def escalate(self, reason, suggested_action="") -> dict:
        self.outcome = _outcome(False, message=reason, escalated=True)
        return {"escalated": True, "reason": reason, "suggested_action": suggested_action}

    def remember(self, scope, subject, fact) -> dict:
        if scope not in MEMORY_SCOPES:
            return {"error": f"scope must be one of {', '.join(MEMORY_SCOPES)}"}
        if not (subject and fact):
            return {"error": "remember needs a subject and a fact"}
        mid = self._db.remember(scope=scope, subject=subject, fact=fact, source="agent")
        return {"ok": True, "memory_id": mid, "scope": scope, "subject": subject, "fact": fact}


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------


def _plain(value):
    """Whatever the SDK handed us, as something json.dumps will take."""
    return json.loads(json.dumps(value, default=str))


def _text(steps: list, text: str) -> None:
    steps.append({"type": "text", "name": None, "content": text})


def _do(tools: Tools, steps: list, name: str, args: dict) -> dict:
    steps.append({"type": "tool_use", "name": name, "content": _plain(args)})
    out = tools.call(name, args)
    steps.append({"type": "tool_result", "name": name, "content": _plain(out)})
    return out


# ---------------------------------------------------------------------------
# Scripted mode
# ---------------------------------------------------------------------------

# "just the frame, not the prints" has to mean one line, not both, so a cue
# turns the rest of its clause into the list of things to leave alone.
_NEGATORS = {"not", "except", "excluding", "besides", "but", "keep", "keeping", "minus"}


def _words(text: str) -> list[str]:
    return [w for w in re.split(r"[^a-z0-9]+", (text or "").lower()) if w]


def _stem(word: str) -> str:
    return word[:-1] if len(word) > 3 and word.endswith("s") else word


def _split_intent(message: str) -> tuple[set[str], set[str]]:
    """The message as two word sets: what is wrong, and what is fine."""
    wanted: set[str] = set()
    refused: set[str] = set()
    for clause in re.split(r"[,.;:!?]+", message or ""):
        bucket = wanted
        for word in _words(clause):
            if word in _NEGATORS:
                bucket = refused
                continue
            bucket.add(_stem(word))
    return wanted, refused


def _keywords(line: dict) -> set[str]:
    raw = f"{line.get('description') or ''} {line.get('id') or ''} {line.get('sku') or ''}"
    return {_stem(w) for w in _words(raw) if len(w) >= 3}


def _run_scripted(tools: Tools, capture_id: str, message: str, steps: list) -> None:
    order = _do(tools, steps, "get_order", {"capture_id": capture_id})
    if "error" in order:
        _text(steps, f"Nothing to read: {order['error']}.")
        return

    buyer = order.get("buyer_email") or "global"

    # Recall BEFORE the decision, every time. This is the step that makes the
    # same sentence from two customers get two answers.
    memory = _do(tools, steps, "recall", {"subject": buyer})
    seen = memory["refunds_90d"]["count"]
    notes = [f["fact"] for f in memory.get("facts", [])]
    said = f"Memory on {buyer}: {seen} refund(s) in the last 90 days."
    if notes:
        said += " " + " ".join(notes[:2])
    _text(steps, said)

    wanted, refused = _split_intent(message)
    picked, held_back = [], []
    for line in order.get("lines", []):
        keys = _keywords(line)
        if keys & refused:
            held_back.append(line["id"])
        elif keys & wanted:
            picked.append(line["id"])

    if not picked:
        _text(steps, "The message names nothing on this order. Moving no money.")
        return

    amount = sum(ln["amount_minor"] for ln in order["lines"] if ln["id"] in picked)
    note = f"The message points at {', '.join(picked)}."
    if held_back:
        note += f" It says {', '.join(held_back)} is fine, so that stays."
    _text(steps, note)

    verdict = _do(tools, steps, "check_policy", {"amount_minor": amount, "buyer_email": buyer})
    reason = " ".join((message or "").split())[:200]

    if not verdict["allowed"]:
        why = "; ".join(verdict["reasons"])
        _text(steps, f"Policy says no: {why}. Handing this to a person.")
        _do(
            tools,
            steps,
            "escalate",
            {
                "reason": why,
                "suggested_action": f"refund {', '.join(picked)} ({to_provider(amount, order['currency'])} "
                                    f"{order['currency']}) if a human agrees",
            },
        )
        _do(
            tools,
            steps,
            "remember",
            {"scope": "decision", "subject": buyer,
             "fact": f"Escalated instead of refunding {', '.join(picked)} on {capture_id}: {why}."},
        )
        return

    _do(tools, steps, "refund_lines",
        {"capture_id": capture_id, "line_ids": picked, "reason": reason})

    if tools.outcome and tools.outcome["ok"]:
        _do(tools, steps, "remember",
            {"scope": "decision", "subject": buyer,
             "fact": f"Refunded {', '.join(picked)} on {capture_id}: {reason}"})
    _do(tools, steps, "get_refund_status", {"capture_id": capture_id})


# ---------------------------------------------------------------------------
# Claude mode
# ---------------------------------------------------------------------------


def _opening(capture_id: str, message: str) -> str:
    return f"Capture id: {capture_id}\n\nCustomer message:\n{message}"


def _run_claude(tools: Tools, capture_id: str, message: str, steps: list) -> None:
    import anthropic

    client = anthropic.Anthropic()
    messages = [{"role": "user", "content": _opening(capture_id, message)}]

    for _ in range(MAX_ITERATIONS):
        response = client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=SYSTEM,
            tools=TOOLS,
            output_config={"effort": "medium"},
            messages=messages,
        )
        # The whole content list goes back, thinking blocks included.
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "refusal":
            detail = getattr(response.stop_details, "explanation", None) or ""
            _text(steps, f"The model declined to answer. {detail}".strip())
            return

        results = []
        for block in response.content:
            if block.type == "text" and block.text.strip():
                _text(steps, block.text.strip())
            elif block.type == "tool_use":
                args = _plain(block.input)
                steps.append({"type": "tool_use", "name": block.name, "content": args})
                out = tools.call(block.name, args)
                steps.append({"type": "tool_result", "name": block.name, "content": _plain(out)})
                # A refused refund is an answer, not a tool failure, so it goes
                # back as an ordinary result and the model reads why.
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": json.dumps(out, default=str),
                    }
                )

        if response.stop_reason != "tool_use" or not results:
            return
        # Every result in ONE user message, or the model stops batching calls.
        messages.append({"role": "user", "content": results})

    _text(steps, f"Stopped after {MAX_ITERATIONS} turns without finishing.")


# ---------------------------------------------------------------------------


def _nothing_moved(steps: list) -> dict:
    """No money moved, so the last thing the agent said is the explanation."""
    out = dict(NO_REFUND)
    said = next(
        (s["content"] for s in reversed(steps) if s["type"] == "text" and s["content"]),
        None,
    )
    if said:
        out["message"] = said
    return out


def run_agent(db, pp, *, capture_id: str, message: str) -> dict:
    tools = Tools(db, pp)
    steps: list = []
    mode = "claude" if (os.getenv("ANTHROPIC_API_KEY") or "").strip() else "scripted"

    if mode == "claude":
        _run_claude(tools, capture_id, message, steps)
    else:
        _run_scripted(tools, capture_id, message, steps)

    return {
        "mode": mode,
        "steps": steps,
        "result": dict(tools.outcome) if tools.outcome else _nothing_moved(steps),
    }
