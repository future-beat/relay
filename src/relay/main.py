import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from html import escape

from anthropic import AsyncAnthropic
from fastapi import Depends, FastAPI, HTTPException, Request, Security
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse

from . import __version__
from .agent import run_ticket
from .auth import Tier, api_key_header, require_tier
from .config import settings
from .db import connect, init_db
from .guardrails import ToolPolicy
from .models import Ticket, TicketCreate
from .ratelimit import enforce, enforce_daily_budget, release_run, reserve_run
from .runs import RegistryDraining, RunRegistry
from .telemetry import configure_logging, record_run, run_metrics, setup_tracing
from .tools import build_registry

logger = logging.getLogger("relay.main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    setup_tracing()
    conn = connect(settings.db_path)
    init_db(conn)
    app.state.conn = conn
    app.state.registry = build_registry(conn, settings.kb_dir)
    app.state.client = AsyncAnthropic(api_key=settings.anthropic_api_key)
    app.state.runs = RunRegistry()
    yield
    # Drained before the connection closes. uvicorn cancels in-flight request tasks
    # when its graceful-shutdown window expires and then runs lifespan shutdown
    # without awaiting those cancellations, so a stream's finally — which writes the
    # run's cost that SEC-03's daily ceiling reads back — can still be pending here.
    # Closing first turns that write into "Cannot operate on a closed database" and
    # loses the row, which is spend the ceiling can never see again.
    await app.state.runs.drain(timeout=settings.shutdown_drain_seconds)
    conn.close()


app = FastAPI(title="Relay", version=__version__, lifespan=lifespan)


# D-07 permits both tiers on every protected surface, so one shared resolver covers
# all three gates. Both are built once at module level: ruff's B008 rightly rejects
# a call in an argument default (auth.py is the same pattern).
_ANY_TIER = require_tier("owner", "demo")
_API_KEY = Security(api_key_header)

# One string for both refusals below. The handler rejects with it before the response
# starts; the generator has to deliver the same refusal in-stream when a drain lands
# after that check. Sharing the copy is what stops the two from drifting apart.
_SHUTTING_DOWN_NOTE = (
    "Relay is finishing the runs already in flight before it restarts."
    " Retry in a few seconds — this is a deploy, not an outage."
)


def _gate(bucket: str, *, meter_spend: bool = False):
    """Build the perimeter dependency for one route: an anonymous per-IP meter,
    then auth, then the daily spend ceiling on costly routes, then the tiered
    per-IP window.

    Every control is a route dependency and never middleware: a StreamingResponse
    locks its status line at 200 once the generator yields, so a rejection raised
    any later than this could only surface as an in-stream error on a 200.

    The anon bucket is charged before the credential is resolved, and the resolver
    is called by hand rather than nested as a sub-dependency, because FastAPI
    resolves sub-dependencies before the body that would meter them: a 401 raised
    from there consumed no allowance at all, leaving the key itself open to
    unlimited online guessing.

    The ceiling is checked before the tiered window because it is a global
    condition — a budget outage should not also burn the caller's per-IP allowance.
    """

    async def _dependency(request: Request, presented: str | None = _API_KEY) -> Tier:
        await enforce("auth", "anon", request)
        tier = _ANY_TIER(presented)
        if meter_spend:
            # Offloaded, because the cost here is acquiring Database's lock, not
            # running the query. The SUM is microseconds behind idx_runs_created_at,
            # but a worker thread holds that lock for a whole transaction() — a
            # measured 0.81s loop stall, bounded only by busy_timeout (5s). The
            # container HEALTHCHECK times out at 3s, so a stalled loop that cannot
            # answer /health gets the machine restarted, killing every in-flight run.
            # HTTPException raised in the thread propagates back through to_thread.
            await asyncio.to_thread(enforce_daily_budget, app.state.conn)
        await enforce(bucket, tier, request)
        return tier

    return _dependency


# D-07: these three cover every mutating or paid surface; /, /health, /metrics and
# /dashboard stay public so the container HEALTHCHECK and the CI smoke job keep working.
create_gate = _gate("create")
read_gate = _gate("read")
process_gate = _gate("process", meter_spend=True)


@app.get("/", include_in_schema=False)
async def root() -> RedirectResponse:
    """Send visitors to the dashboard rather than a bare 404."""
    return RedirectResponse("/dashboard")


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "version": __version__, "model": settings.model}


@app.post(
    "/tickets",
    response_model=Ticket,
    status_code=201,
    dependencies=[Depends(create_gate)],
)
async def create_ticket(payload: TicketCreate) -> Ticket:
    def _insert() -> int:
        with app.state.conn.transaction() as db:
            cur = db.execute(
                "INSERT INTO tickets (customer_email, subject, body) VALUES (?, ?, ?)",
                (payload.customer_email, payload.subject, payload.body),
            )
            # Read inside the block: once the lock drops another thread's insert has
            # already moved lastrowid, and this row's id is what the caller gets back.
            return cur.lastrowid

    return await _get_ticket(await asyncio.to_thread(_insert))


@app.get("/tickets/{ticket_id}", response_model=Ticket, dependencies=[Depends(read_gate)])
async def get_ticket(ticket_id: int) -> Ticket:
    return await _get_ticket(ticket_id)


@app.post("/tickets/{ticket_id}/process", dependencies=[Depends(process_gate)])
async def process_ticket(ticket_id: int, dry_run: bool = False) -> StreamingResponse:
    """Run the agent on a ticket, streaming each step as a server-sent event.

    With dry_run=true, write-tier tools are denied by policy: the agent can
    read data and search docs but cannot reply, escalate, or categorise.
    Every run is recorded in the runs table for /metrics.
    """
    ticket = await _get_ticket(ticket_id)
    if ticket.status != "open":
        raise HTTPException(409, f"ticket is already {ticket.status.value}")

    # Refused before the reservation below, so a rejected caller never claims spend
    # it will not use. A run admitted mid-drain would extend the shutdown window and
    # can outlive conn.close(). Best-effort by nature — uvicorn stops accepting
    # connections before lifespan runs, so the window this closes is narrow, which is
    # why it is one check and not a mechanism. The dict detail follows ratelimit.py's
    # perimeter convention rather than the short-string domain form, and carries no
    # active counts, ticket ids or timeouts.
    if app.state.runs.draining:
        raise HTTPException(
            503,
            detail={"error": "shutting_down", "note": _SHUTTING_DOWN_NOTE},
            headers={"Retry-After": "5"},
        )

    # Claim this run's worst-case cost now that the gate has admitted it. record_run
    # only fires once the stream ends, so without a reservation a burst of concurrent
    # runs would all read the same stale SUM and all clear the daily ceiling. The
    # token is claimed here but released below, and the two can be separated by a
    # cancellation that skips the release entirely — which is why the claim expires
    # on its own rather than trusting this handoff.
    token = reserve_run()

    async def event_stream():
        started = time.perf_counter()
        # Registered here rather than beside reserve_run() above, and the difference
        # is the whole point: Starlette can cancel a StreamingResponse before its
        # generator ever starts, and a finally in a generator whose body never ran
        # does not execute. Registering outside the body would leak an entry on every
        # aborted request, stalling every later drain for its full grace period and
        # leaving an idle server non-empty — which is what breaks scale-to-zero.
        # Inside the body, register and deregister are exactly balanced, which is why
        # this needs no TTL where the reservation above does.
        try:
            run_token = app.state.runs.register(ticket_id=ticket.id)
        except RegistryDraining:
            # The handler's 503 above ran before this generator was scheduled, and the
            # drain landed in between — it saw an empty registry, took its fast path,
            # and the connection is closing. Refused here rather than started, because
            # a StreamingResponse locks its status line at 200 once the body begins:
            # the same refusal can only be delivered as an in-stream event now.
            # Released explicitly because the finally below belongs to a run that never
            # started, and returning from here skips it.
            release_run(token)
            logger.info("run.refused_after_drain", extra={"ctx": {"ticket_id": ticket.id}})
            refusal = {"reason": "shutting_down", "note": _SHUTTING_DOWN_NOTE}
            yield f"event: error\ndata: {json.dumps(refusal)}\n\n"
            return
        usage: dict = {}
        outcome = "incomplete"
        recorded = False
        try:
            async for event in run_ticket(
                app.state.client,
                app.state.registry,
                ticket.model_dump(),
                policy=ToolPolicy(allow_writes=not dry_run),
            ):
                if event.type == "usage":
                    usage = event.data
                elif event.type == "resolution":
                    outcome = event.data["via"] or "dry_run_complete"
                elif event.type == "error":
                    outcome = f"error:{event.data['reason']}"
                yield f"event: {event.type}\ndata: {json.dumps(event.data)}\n\n"
            yield "event: done\ndata: {}\n\n"
        finally:
            # A plain finally, never a context manager: run_ticket suspends at every
            # yield, and anything held across a yield leaks into whatever coroutine
            # runs in between (see agent.py's run-span note).
            #
            # The row is written from here rather than after the loop because a client
            # that disconnects mid-stream cancels this generator at its suspended
            # yield: every Claude call already made is real money, and the daily
            # ceiling reads it back out of `runs`. An aborted run keeps `outcome`
            # "incomplete", which is also the honest value for /metrics. The flag
            # keeps a second close from writing the row twice and double-charging
            # the ledger.
            #
            # The write is wrapped because it is the one statement here that talks to
            # the database, and the likeliest way it fails is the race this phase
            # closes: a drain times out, lifespan closes the connection, and this
            # write raises "Cannot operate on a closed database". Unguarded, that
            # exception would skip both cleanups below — leaking the registry entry
            # permanently, so every later drain burns its full grace period and
            # returns False, and stranding $0.50 of the daily ceiling until its TTL.
            # Losing one telemetry row must not cost the process its drain.
            try:
                if not recorded:
                    recorded = True
                    record_run(
                        app.state.conn,
                        ticket_id=ticket.id,
                        model=settings.model,
                        duration_ms=int((time.perf_counter() - started) * 1000),
                        steps=usage.get("steps", 0),
                        input_tokens=usage.get("input_tokens", 0),
                        output_tokens=usage.get("output_tokens", 0),
                        cost_usd=usage.get("cost_usd", 0.0),
                        outcome=outcome,
                    )
            except Exception:
                logger.exception("run.record_failed", extra={"ctx": {"ticket_id": ticket.id}})
            finally:
                # Released after the row exists, so the two are never both missing.
                release_run(token)
                # Last, so a drain waiting on this run only wakes once its row is written.
                app.state.runs.deregister(run_token)

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.get("/metrics")
async def metrics() -> dict:
    # run_metrics does SELECT * FROM runs, the one read here that grows unbounded:
    # the dashboard polls this every 5s, so it is the last place to hold the loop.
    return await asyncio.to_thread(run_metrics, app.state.conn)


DASHBOARD_HTML = """<!doctype html>
<html><head><title>Relay dashboard</title><style>
body { font-family: ui-monospace, monospace; max-width: 900px; margin: 2rem auto; padding: 0 1rem; }
h1 { font-size: 1.2rem; } .cards { display: flex; gap: 1rem; flex-wrap: wrap; }
.card { border: 1px solid #ccc; border-radius: 8px; padding: .8rem 1.2rem; min-width: 140px; }
.card b { display: block; font-size: 1.4rem; } .card span { color: #666; font-size: .8rem; }
table { border-collapse: collapse; width: 100%; margin-top: 1.5rem; font-size: .85rem; }
th, td { text-align: left; padding: .3rem .6rem; border-bottom: 1px solid #eee; }
.demo { border: 1px solid #ccc; border-left: 4px solid #444; border-radius: 8px;
        padding: .8rem 1.2rem; margin-bottom: 1.5rem; font-size: .85rem; }
.demo code { background: #f4f4f4; padding: .1rem .3rem; border-radius: 4px; }
.demo em { color: #666; font-style: normal; }
</style></head><body>
<h1>Relay — agent runs</h1>
<div class="demo">
Try it yourself — this key is published on purpose:
<code>X-API-Key: __RELAY_DEMO_KEY__</code><br>
<em>Deliberately limited: 5 runs/hour per IP, and the demo caps Claude spend at $5/day.</em>
</div>
<div class="cards" id="cards"></div>
<table id="runs"><thead><tr><th>id</th><th>ticket</th><th>outcome</th><th>steps</th>
<th>tokens in/out</th><th>cost</th><th>ms</th><th>at</th></tr></thead><tbody></tbody></table>
<script>
async function refresh() {
  const m = await (await fetch("/metrics")).json();
  document.getElementById("cards").innerHTML = [
    ["runs", m.runs], ["p50 ms", m.latency_ms.p50], ["p95 ms", m.latency_ms.p95],
    ["total cost", "$" + m.cost_usd.total], ["mean cost", "$" + m.cost_usd.mean_per_run],
    ["tokens in", m.tokens.input], ["tokens out", m.tokens.output],
  ].map(([label, value]) => `<div class="card"><b>${value}</b><span>${label}</span></div>`).join("");
  document.querySelector("#runs tbody").innerHTML = m.last_runs.map(r =>
    `<tr><td>${r.id}</td><td>#${r.ticket_id}</td><td>${r.outcome}</td><td>${r.steps}</td>
     <td>${r.input_tokens}/${r.output_tokens}</td><td>$${r.cost_usd.toFixed(4)}</td>
     <td>${r.duration_ms}</td><td>${r.created_at}</td></tr>`).join("");
}
refresh(); setInterval(refresh, 5000);
</script></body></html>"""


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard() -> str:
    """Serve the dashboard, substituting the demo key at request time.

    D-02 publishes the demo key here and in the README. Substituting from
    settings rather than baking a literal into DASHBOARD_HTML means the page
    can never advertise a key the service would reject. A .replace() and not an
    f-string: the inline JS is full of ${...} template literals.

    An unconfigured deployment renders a neutral placeholder — /dashboard is the
    public landing surface and must not print "None" as if it were a credential.
    """
    published = escape(settings.demo_key) if settings.demo_key else "(not configured)"
    return DASHBOARD_HTML.replace("__RELAY_DEMO_KEY__", published)


async def _get_ticket(ticket_id: int) -> Ticket:
    # fetchone() is called inside the offloaded callable, not after it: Database
    # materialises rows while its lock is held, and stepping the result back on the
    # event loop would put the read half a statement outside the thread it belongs to.
    row = await asyncio.to_thread(
        lambda: app.state.conn.execute(
            "SELECT * FROM tickets WHERE id = ?", (ticket_id,)
        ).fetchone()
    )
    if row is None:
        raise HTTPException(404, "ticket not found")
    return Ticket(**dict(row))
