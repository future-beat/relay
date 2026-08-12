import asyncio
import json
import logging
import time
import uuid
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
from .events import _CLOSE_SENTINEL, BrokerUnavailable, RunEventBroker, RunRecorder, project
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
    # Beside the registry and for the same reason: it holds asyncio.Queues, which bind
    # to the loop they are first used on, so building one at import would tie every
    # subscriber to whichever loop imported the module (runs.py L60-64). Deliberately
    # NOT folded into RunRegistry — a watching browser is not work to drain (D-12).
    app.state.broker = RunEventBroker()
    yield
    # Drained before the connection closes. uvicorn cancels in-flight request tasks
    # when its graceful-shutdown window expires and then runs lifespan shutdown
    # without awaiting those cancellations, so a stream's finally — which writes the
    # run's cost that SEC-03's daily ceiling reads back — can still be pending here.
    # Closing first turns that write into "Cannot operate on a closed database" and
    # loses the row, which is spend the ceiling can never see again.
    await app.state.runs.drain(timeout=settings.shutdown_drain_seconds)
    # Beside the drain, never inside it: the drain waits for paid runs to finish, and a
    # watching browser is not work to wait for (D-12). Closing here wakes every open
    # /events generator with the sentinel so it returns now rather than sitting out its
    # idle ceiling — an open SSE stream is an in-flight connection uvicorn holds the
    # graceful-shutdown window open for, and one Fly's proxy counts when deciding
    # whether the machine may autostop.
    #
    # After the drain (a run still streaming must keep publishing to its subscribers)
    # and before conn.close() (a generator woken here may still touch nothing, but the
    # ordering matches the drain's own close-last discipline above).
    app.state.broker.close()
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


def _gate(bucket: str, *, meter_spend: bool = False, public: bool = False):
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
    That ordering used to mean the 503 short-circuited throttling entirely: the
    tiered window below is never reached once the ceiling raises, so /process
    became an unthrottled endpoint for anyone holding the published demo key,
    precisely when the service was least able to defend itself. The refusal now
    charges its own bucket on the way out, which keeps both properties.

    `public=True` builds the same perimeter minus the credential: the anon meter and
    then the route's own bucket keyed on "anon". It is for surfaces D-11 keeps open on
    purpose (the live feed), whose safety is content control rather than access
    control — an unmetered public surface is still a free-work surface, and on a
    connection-holding route it is the thing that defeats min_machines_running=0.
    """

    async def _dependency(request: Request, presented: str | None = _API_KEY) -> Tier | None:
        await enforce("auth", "anon", request)
        if public:
            # No tier to resolve and none to return: the caller presented nothing, and
            # metering an anonymous bucket is the whole of this gate.
            await enforce(bucket, "anon", request)
            return None
        tier = _ANY_TIER(presented)
        if meter_spend:
            # Offloaded, because the cost here is acquiring Database's lock, not
            # running the query. The SUM is microseconds behind idx_runs_created_at,
            # but a worker thread holds that lock for a whole transaction() — a
            # measured 0.81s loop stall, bounded only by busy_timeout (5s). The
            # container HEALTHCHECK times out at 3s, so a stalled loop that cannot
            # answer /health gets the machine restarted, killing every in-flight run.
            # HTTPException raised in the thread propagates back through to_thread.
            try:
                await asyncio.to_thread(enforce_daily_budget, app.state.conn)
            except HTTPException:
                # Metered after the fact, not before: the bucket must only be spent by
                # requests the ceiling actually refused, so a healthy service never
                # touches it. A 429 from here replaces the 503, which is the intended
                # answer to a retry loop — the 503's copy is for a visitor, not a flood.
                await enforce("process", "outage", request)
                raise
        await enforce(bucket, tier, request)
        return tier

    return _dependency


# D-07: these three cover every mutating or paid surface; /, /health, /metrics and
# /dashboard stay public so the container HEALTHCHECK and the CI smoke job keep working.
create_gate = _gate("create")
read_gate = _gate("read")
process_gate = _gate("process", meter_spend=True)
# Public (D-11) but not unmetered: /events holds a connection open for its whole idle
# ceiling, which is exactly what a reconnect loop needs to keep the machine awake.
events_gate = _gate("events", public=True)


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
        # This run's identity, minted before the first event so every run_events row
        # and the runs row written in the finally below carry the same key. Not the
        # runs row's id: that row does not exist until the stream ends, long after the
        # first event has to be written (hence a soft join key, not a foreign one).
        run_uid = uuid.uuid4().hex
        recorder = RunRecorder(app.state.conn, run_uid=run_uid, ticket_id=ticket.id)
        try:
            async for event in run_ticket(
                app.state.client,
                app.state.registry,
                ticket.model_dump(),
                policy=ToolPolicy(allow_writes=not dry_run),
                recorder=recorder,
            ):
                if event.type == "usage":
                    usage = event.data
                elif event.type == "resolution":
                    outcome = event.data["via"] or "dry_run_complete"
                elif event.type == "error":
                    outcome = f"error:{event.data['reason']}"
                # The public mirror. `project()` is the only path event data may take
                # to a subscriber, and it is an allowlist: None means "nothing safe to
                # show for this event", which is a drop, never an empty frame.
                #
                # Post-commit by construction (D-06), not by an ordering rule anyone
                # has to remember: run_ticket persists each event before it yields it,
                # so arriving here already means committed. Publishing from inside the
                # agent loop, or writing the row here, would both break that.
                frame = project(event)
                if frame is not None:
                    app.state.broker.publish(frame)
                # Unchanged, and deliberately NOT the projection: this stream belongs to
                # the caller who owns the ticket and stays full-fidelity. Only the
                # broadcast fan-out above is redacted.
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
                        # Stamped so this summary row joins the per-step rows already
                        # written under the same uid — the whole of phase 6's drill-down.
                        run_uid=run_uid,
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


def _snapshot_frame() -> str:
    """The one frame a new subscriber gets before any live event (D-14).

    Without it a tab opened mid-run shows an empty feed until the next step happens,
    which on a quiet demo can be minutes — the visitor concludes nothing is running.

    Built field by field from ActiveRun, for the same reason project() is: this is a
    public boundary, and `asdict()` here would publish whatever field someone adds to
    ActiveRun later. There are exactly two fields today and neither is a secret —
    ticket_id is already public on /metrics (last_runs), and started_at is deliberately
    NOT published raw: it is a monotonic clock reading, meaningless off this process,
    so it is rendered as the elapsed time a viewer can actually use.

    Not routed through project(): that function's input is an AgentEvent, and this is
    registry state, not a run event. It is still an allowlist, written out below.
    """
    now = time.monotonic()
    runs = [
        {"ticket_id": run.ticket_id, "running_for_ms": int((now - run.started_at) * 1000)}
        for run in app.state.runs.snapshot()
    ]
    return f"event: snapshot\ndata: {json.dumps({'type': 'snapshot', 'runs': runs})}\n\n"


@app.get("/events", dependencies=[Depends(events_gate)])
async def events() -> StreamingResponse:
    """The public live feed: every run's redacted steps, as they happen (D-11, SC-2).

    No API key, deliberately — it joins /metrics and /dashboard as a public surface.
    Its safety is content control, not access control: every frame here was built by
    project()'s allowlist before it reached the broker, so there is nothing to
    authorise. Do not add a second serialisation path around that.

    Public is not the same as unbounded, and this route is the one that makes the
    difference matter: it holds a connection open for its whole idle ceiling, and the
    machine may only autostop when none are held. So it is bounded twice — a per-IP
    window on connects (events_gate above, a route dependency and never middleware,
    because a StreamingResponse locks its status at 200 on first yield), and a hard
    ceiling on concurrent subscribers below, since publish() is O(subscribers) on the
    same loop that answers the container HEALTHCHECK.
    """
    if app.state.broker.at_capacity:
        # Refused here rather than inside the generator: this is the last point where a
        # rejection can still be an HTTP status code. Nothing is subscribed on this
        # path, so a refusal cannot leak a queue.
        logger.info("events.refused_at_capacity", extra={"ctx": {}})
        raise HTTPException(
            503,
            detail={
                "error": "too_many_viewers",
                "note": (
                    "The live feed is at its viewer limit. Try again in a moment —"
                    " this is a deliberate cost control on the public demo, not an outage."
                ),
            },
            headers={"Retry-After": "30"},
        )

    async def stream():
        # First statement of the body, and the reason is event_stream's L200-209 note:
        # Starlette can cancel a StreamingResponse before its generator ever starts, and
        # a finally in a body that never ran does not execute. Subscribing one line
        # higher (in the handler) would leak a queue on every aborted connection, and a
        # leaked subscriber is worse than a leaked registry entry — publish keeps
        # writing to a queue nobody reads for the life of the process (CR-02).
        try:
            q = app.state.broker.subscribe()
        except BrokerUnavailable:
            # The handler's check above passed and another connection took the last
            # slot before this body was scheduled. The status line is already 200, so
            # the only refusal left is in-stream: a comment (never an event — nothing
            # here went through project()) and an immediate close. No queue exists on
            # this path, so the finally below must not run — hence the return here
            # rather than a flag threaded through it.
            logger.info("events.refused_at_capacity", extra={"ctx": {"raced": True}})
            yield ": at-capacity\n\n"
            return
        # A viewer is NOT registered in RunRegistry (D-12). The phase-2 drain waits for
        # `active == 0`; a counted viewer would hold shutdown open for its full grace
        # period and pin the machine awake — the opposite of what this route's idle
        # ceiling exists to guarantee.
        try:
            idle_deadline = time.monotonic() + settings.events_idle_seconds
            yield _snapshot_frame()
            while True:
                try:
                    frame = await asyncio.wait_for(
                        q.get(), timeout=settings.events_heartbeat_seconds
                    )
                except TimeoutError:
                    # The idle ceiling measures RUN activity, and this branch is the
                    # absence of it. Fly autostops on active connections, not container
                    # load, so a forgotten tab holds the machine awake until the app
                    # itself lets go — closing here is the whole of that guarantee, and
                    # EventSource reconnects on its own when the viewer comes back.
                    if time.monotonic() >= idle_deadline:
                        logger.info("events.idle_closed", extra={"ctx": {}})
                        return
                    # A comment, not an event: it keeps proxies and EventSource from
                    # timing a quiet stream out. It must NEVER touch idle_deadline —
                    # a keep-alive that renews its own deadline is a stream that can
                    # never idle-close, which silently defeats min_machines_running=0.
                    yield ": keep-alive\n\n"
                    continue
                if frame is _CLOSE_SENTINEL:
                    return  # lifespan called broker.close(); end before conn.close()
                # Reset on a real frame only — see above.
                idle_deadline = time.monotonic() + settings.events_idle_seconds
                yield f"event: {frame['type']}\ndata: {json.dumps(frame)}\n\n"
        finally:
            # Every exit path lands here: client disconnect (cancellation at a yield),
            # idle close, the shutdown sentinel, or an exception. Idempotent, so the
            # double-unsubscribe a close() plus a disconnect can produce is harmless.
            app.state.broker.unsubscribe(q)

    return StreamingResponse(stream(), media_type="text/event-stream")


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
