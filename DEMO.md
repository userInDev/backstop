# The demo

Three minutes, one idea: **the agent is allowed to move money and cannot move it twice.**

Everything below runs with no PayPal credentials and no network. That is deliberate. A judge should
be able to reproduce the whole thing from a clone.

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m pytest tests -q          # 11 passed
uvicorn backstop.api:app --port 8000
```

Open http://localhost:8000.

## The beats

**0:00 to 0:20. The problem.**
Not "can an agent pay", but "what does an agent do when the connection drops while it is paying".
Say the three buckets out loud: refused, succeeded, and the one nobody builds, unknown.

**0:20 to 1:00. The agent does its job.**
Customer message: *"the frame arrived cracked, just the frame, not the prints."*
Send to agent. The transcript shows it call `get_order`, see two lines, and refund only the frame.
The grid gains a green `succeeded` row for 40.00 of a 120.00 order. It did not refund the prints and
it did not guess an amount.

**1:00 to 2:00. The dangerous case.**
Reset. Turn **CHAOS** on, which really sends the refund and throws the response away. That is what a
dropped connection leaves you holding.

- The attempt lands **red, `unknown`**.
- Say the line that matters: *PayPal may or may not have recorded this. We genuinely do not know.*
- Ask the agent again, exactly as a real retry would. **It is refused.** Rule 1 in `core.py`.
- Point out that `pp.refund_calls == 1` in the test suite proves this is not cosmetic.

**2:00 to 2:40. Resolution.**
Hit **Reconcile**. It reads the provider's own refund list, matches on `custom_id`, and settles the
attempt as `succeeded`. One refund exists. Not two.
Say why only an answer may settle: if the list read itself fails, the attempt stays unknown, because
a timeout is not evidence of absence.

**2:40 to 3:00. The close.**
`PayPal-Request-Id` protects PayPal from a duplicate request. It does nothing for the agent's own
uncertainty. Backstop is about what the caller knows. Cut to the four rules in `core.py`.

## Against the real sandbox

Same beats, swapping the provider:

```bash
BACKSTOP_PROVIDER=paypal uvicorn backstop.api:app --port 8000
```

Needs `.env` filled in, and a capture created first via `scripts/sandbox.py`. Shoot the fake-provider
version for the timing, then show the real PayPal dashboard with exactly one refund on it as proof.

## What not to do on camera

- Do not explain SQLite, FastAPI or AG Grid. Nobody is scoring the stack.
- Do not read the four rules aloud in order. Show one being enforced.
- Do not apologise for the fake provider. It is the reason the judge can run it.
