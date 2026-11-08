# Backstop

An AI agent that can issue PayPal refunds, and structurally cannot issue the same one twice.

A customer writes *"the frame arrived cracked, just the frame, not the prints."* The agent reads it,
decides a partial refund, and executes it against PayPal. Every money-moving action goes through a
safety layer. That layer is the project.

## The problem

Agentic commerce demos fail in the same place. Not when the agent pays, but when the connection
drops while it is paying.

The agent asks PayPal for a refund. The response never arrives. The agent now knows nothing, and an
agent that knows nothing does something wrong next: it retries and pays twice, or it tells the
customer the refund failed when the money is already gone, or it loops.

Everything a payment provider can do to you falls into three buckets, and conflating any two of them
is how money moves twice:

| Outcome | What happened | Safe to retry |
|---|---|---|
| **Refused** | PayPal answered and said no. Nothing moved | Yes |
| **Unknown** | Timeout, 5xx, dropped connection, process killed | **No** |
| **Succeeded** | PayPal answered and recorded it | n/a |

Most code has two buckets. `unknown` is the one nobody builds, and it is the only one that matters.

## PayPal-Request-Id is not enough

PayPal gives you an idempotency header, and Backstop sends it. It is not sufficient, and saying why
is half the point.

`PayPal-Request-Id` protects **PayPal** from processing a duplicate request. It does nothing for the
**agent's** uncertainty. When the response never arrives, we still do not know whether a refund
exists. Deduplication is a property of the provider. Knowing is a property of the caller, and only
the caller can be held responsible for what it tells the customer next.

## The four rules

All four live in [`backstop/core.py`](backstop/core.py). They are code in front of the money, not
instructions in a prompt, so there is no wording the agent can choose that gets past them.

1. **Refuse while anything is unresolved.** An attempt in `pending` or `unknown` on this capture may
   already have moved money. Nobody tries again until the reconciler settles it.

2. **Refuse a line somebody already claimed.** The provider's cap guards the *total*, not the item.
   Refunding one cracked frame three times never exceeds the order total, so PayPal will allow it.

3. **Read the balance live from PayPal, never from our own table.** A refund issued by hand in the
   PayPal dashboard leaves no row here. The live read is also what makes a double submit safe.

4. **Write the attempt row before the call.** A row written afterwards is a row that never gets
   written in exactly the case that matters: the process dying while PayPal is thinking.

And one rule for the reconciler, in [`backstop/reconcile.py`](backstop/reconcile.py): **only an
answer may settle an unknown.** If the list read itself fails, the attempt stays unknown. A timeout
is not evidence of absence.

## Run it

No hosting, no Docker, no Postgres. SQLite on purpose, so you can run this in ten seconds.

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m pytest tests -q
```

Eleven tests, no network, no credentials. Each one is a way money moves twice if you delete the rule
it covers. The one to read is `test_unknown_blocks_the_second_refund`.

## Against the real sandbox

```bash
cp .env.example .env     # paste sandbox REST credentials from developer.paypal.com
python scripts/sandbox.py order          # create an order, print the approve URL
python scripts/sandbox.py capture ORD-ID # after approving as the sandbox buyer
python scripts/sandbox.py refund CAP-ID L-frame
python scripts/sandbox.py refunds ORD-ID # what PayPal itself believes exists
```

## The chaos demo

```bash
BACKSTOP_CHAOS=refund python scripts/sandbox.py refund CAP-ID L-frame
```

The refund request is really sent to PayPal. The response is thrown away, which is exactly what a
dropped connection leaves you holding. The attempt lands in `unknown`.

Retry it and Backstop refuses. Run the reconciler and it reads PayPal's own refund list, matches on
`custom_id`, and settles the attempt as succeeded. Open the PayPal dashboard and there is **one**
refund, not two.

## Layout

| File | What it is |
|---|---|
| `backstop/errors.py` | The three-bucket taxonomy. Read this first |
| `backstop/core.py` | The four rules. The only path to a refund |
| `backstop/reconcile.py` | The only thing allowed to settle an `unknown` |
| `backstop/paypal.py` | The provider boundary, where HTTP becomes one of three buckets |
| `backstop/db.py` | SQLite. Money in minor units as integers, never floats |
| `tests/` | Each test is a way to pay twice |

## Status

Built: the taxonomy, the four rules, the reconciler, the SQLite layer, 11 passing tests, and the
sandbox probe CLI.

Next: the agent and its tools, the AG Grid operations console, and the demo video.

## Licence

MIT. See [LICENSE](LICENSE).
