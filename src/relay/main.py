import asyncio
import json
import logging
import re
import sqlite3
import time
import uuid
from contextlib import asynccontextmanager
from html import escape
from pathlib import Path

from anthropic import AsyncAnthropic
from fastapi import Depends, FastAPI, HTTPException, Request, Security
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse

from . import __version__
from .agent import run_ticket
from .auth import Tier, api_key_header, require_tier
from .config import settings
from .db import connect, init_db, purge_expired_run_events
from .events import (
    _CLOSE_SENTINEL,
    BrokerUnavailable,
    RunEventBroker,
    RunRecorder,
    attribute_to_run,
    mask_withheld,
    project,
    project_run_detail,
    snapshot_frame,
)
from .guardrails import ToolPolicy
from .models import Ticket, TicketCreate
from .ratelimit import (
    budget_snapshot,
    enforce,
    enforce_daily_budget,
    release_run,
    reserve_run,
)
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
    # The retention sweep (WR-05). run_events.payload is raw by design (D-01), so this
    # is the only thing standing between a public demo anyone can drive and an unbounded
    # store of other people's customer emails, ticket bodies and reply text on the Fly
    # volume. At startup rather than on a timer: this process scales to zero by design,
    # so a background task is a task that mostly does not exist, while a boot is the one
    # moment the machine is reliably awake and nothing is streaming yet.
    #
    # Offloaded like every other DB touch — it holds Database's lock for a DELETE over a
    # table that grows by ~10 rows per run, and a slow disk here would stall the loop
    # before it ever answers /health.
    purged = await asyncio.to_thread(
        purge_expired_run_events, conn, retention_days=settings.events_retention_days
    )
    logger.info("run_events.retention_swept", extra={"ctx": {
        "deleted": purged, "retention_days": settings.events_retention_days,
    }})
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

    `public=True` builds the route's OWN bucket keyed on "anon", and nothing else. It is
    for surfaces D-11 keeps open on purpose (the live feed, the drill-down), whose safety
    is content control rather than access control — an unmetered public surface is still
    a free-work surface, and on a connection-holding route it is the thing that defeats
    min_machines_running=0.

    A public route is deliberately NOT charged the shared `auth` bucket (WR-02). That
    bucket exists to meter online KEY GUESSING, and a public gate resolves no credential
    for anyone to guess — while charging it made every public route's own bucket
    unreachable, because `anon_auth_limit` (60/minute) is smaller than the buckets it
    preceded (`anon_run_detail_limit`, 120/minute). The consequence was the exact
    coupling the drill-down's separate bucket was created to prevent: 60 drill-down opens
    spent the live feed's reconnect allowance, /events answered 429, and EventSource
    treats a non-200 as terminal — so clicking through the back catalogue silently killed
    the visitor's own feed. Each public route now meters itself and only itself; this
    also matches `test_public_routes_are_not_charged_the_anon_meter`, which already
    states the property for /health and /metrics.
    """

    async def _dependency(request: Request, presented: str | None = _API_KEY) -> Tier | None:
        if public:
            # No tier to resolve and none to return: the caller presented nothing, and
            # metering this route's own anonymous bucket is the whole of this gate.
            await enforce(bucket, "anon", request)
            return None
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
# The per-run drill-down, public for the same reason and bounded for a different one.
# Its OWN bucket, deliberately not the feed's: a visitor clicking through the back
# catalogue would otherwise spend the live feed's reconnect allowance and silently break
# their own feed, and a drill-down flood has to be visible as itself in the logs rather
# than hidden inside the feed's line.
run_detail_gate = _gate("run_detail", public=True)

# uuid4().hex, which is what process_ticket mints. Lowercase hex, exactly 32 — the shape
# is known precisely, so a scripted walk of made-up keys can be refused for the cost of a
# regex instead of a query (T-06-17).
_RUN_UID_RE = re.compile(r"\A[0-9a-f]{32}\Z")

# The `runs` columns the drill-down envelope publishes. Explicit for the same reason
# _PUBLIC_RUN_COLUMNS is: what a public route discloses is a list someone wrote down.
# `run_uid` is not here (the caller already holds it, and the envelope carries it) and
# neither is `id`, the local autoincrement nobody outside this database needs.
_DETAIL_RUN_COLUMNS = (
    "outcome", "cost_usd", "duration_ms", "steps",
    "input_tokens", "output_tokens", "model", "created_at", "ticket_id",
)

# Declared once at module level for the same reason _ANY_TIER/_API_KEY are: ruff's B008
# rightly rejects a call in an argument default. create_ticket takes the gate as a
# PARAMETER rather than in dependencies=[...] because _gate's dependency returns the
# resolved tier, and that tier is the only server-side signal that a ticket is
# visitor-authored (D-02). A gate declared in dependencies=[...] throws its return value
# away; declared in BOTH places it would run — and meter — the perimeter twice per
# request, silently halving every demo limit in D-04.
_CREATE_GATE = Depends(create_gate)


@app.get("/", include_in_schema=False)
async def root() -> RedirectResponse:
    """Send visitors to the dashboard rather than a bare 404."""
    return RedirectResponse("/dashboard")


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "version": __version__, "model": settings.model}


@app.post("/tickets", response_model=Ticket, status_code=201)
async def create_ticket(payload: TicketCreate, tier: Tier | None = _CREATE_GATE) -> Ticket:
    """Create a ticket, recording the tier that created it (D-02).

    `origin` is the CREATION tier and never the processing one, and the difference is a
    disclosure boundary rather than a naming preference. Full fidelity discloses ticket
    CONTENT — the subject, the body, the raw tool inputs the run saw — and what makes
    that safe is that the visitor holding the published demo key authored that content
    themselves. A flag set from the /process tier instead would make an owner-authored
    ticket, carrying a real customer's words, full fidelity the moment anyone ran it
    with the published key: a disclosure reachable with one curl (threat T-06-15).

    NULL is the legacy value and reads as NOT demo everywhere — the read side compares
    `origin == "demo"` by equality, never truthiness, so an unclassified row fails
    closed.
    """
    def _insert() -> int:
        with app.state.conn.transaction() as db:
            cur = db.execute(
                "INSERT INTO tickets (customer_email, subject, body, origin)"
                " VALUES (?, ?, ?, ?)",
                (payload.customer_email, payload.subject, payload.body, tier),
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

    # This run's identity, minted before the first event so every run_events row and the
    # runs row written in the finally below carry the same key. Not the runs row's id:
    # that row does not exist until the stream ends, long after the first event has to be
    # written (hence a soft join key, not a foreign one).
    #
    # Minted in the HANDLER rather than inside event_stream, so it can also travel back
    # on the response as X-Relay-Run-Uid. Try-it streams this route with `fetch` while
    # simultaneously watching the ambient /events feed, which carries the REDACTED
    # projection of this same run: without the uid the page renders one run twice, in
    # two fidelities, with nothing connecting them — and cannot deep-link its own
    # drill-down. A header touches ZERO of the SSE event contract (the milestone's
    # compatibility constraint) where an extra `event: run` frame would change it, and
    # scripts/demo.sh would print it. Same-origin fetch reads response headers with no
    # CORS allowlist needed.
    run_uid = uuid.uuid4().hex

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
                #
                # Stamped with this run's identity on the way out (CR-03): several runs
                # can be in flight at once, and a frame nobody can attribute is a cost
                # figure without a ticket and a resolution without a run. The stamp is
                # applied here, where the identity is known, and never inside project(),
                # which stays a pure per-event redactor.
                frame = project(event)
                if frame is not None:
                    app.state.broker.publish(
                        attribute_to_run(frame, run_uid=run_uid, ticket_id=ticket.id)
                    )
                # Unchanged, and deliberately NOT the projection: this stream belongs to
                # the caller who owns the ticket and stays full-fidelity. Only the
                # broadcast fan-out above is redacted.
                yield f"event: {event.type}\ndata: {json.dumps(event.data)}\n\n"
            yield "event: done\ndata: {}\n\n"
        except sqlite3.Error:
            # A run_events INSERT failed. This phase introduced the only way that can
            # happen mid-run — before it, no per-event DB write existed, so a lock could
            # not abort a run in flight. busy_timeout is 5s and the MCP server runs
            # against the same file, so a cross-process "database is locked" is a real
            # path here, as is "Cannot operate on a closed database" if a drain times out.
            #
            # The run still ENDS: D-01 makes the database the source of truth and D-06
            # only publishes what is already committed, so carrying on after a failed
            # write would leave the live feed and phase 6's drill-down describing a run
            # that was recorded with a hole in it. A durability failure is not a logging
            # hiccup to swallow.
            #
            # What must not happen — and what this phase regressed — is the exception
            # leaving this generator. That truncated the caller's stream with no
            # explanation at all, against this codebase's rule that a run's failures
            # reach the caller as a structured event and never as a stack trace. So the
            # same fail-closed decision is made, and it is now said out loud in the
            # stream, in the same reason-string form as api_connection_error and
            # budget_exceeded.
            #
            # Caught by type and narrowly: sqlite3.Error is the persistence layer's own
            # base class, and _execute_guarded already absorbs anything a TOOL raises, so
            # the only thing that reaches here is the recorder. It is deliberately NOT
            # published to the broker — the one publish site is the projection above, and
            # a lossy mirror missing a frame is exactly what D-01 allows it to be.
            outcome = "error:persistence_failed"
            logger.exception("run.persistence_failed", extra={"ctx": {
                "ticket_id": ticket.id, "run_uid": run_uid,
            }})
            failure = {
                "reason": "persistence_failed",
                "note": (
                    "Relay could not durably record this run's steps, so it stopped"
                    " rather than continue unrecorded. Work already done is charged;"
                    " retry the ticket."
                ),
            }
            yield f"event: error\ndata: {json.dumps(failure)}\n\n"
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

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        # The uid its submitter needs (above), plus the two headers 05-REVIEW IN-02
        # names. X-Accel-Buffering is nginx's opt-out and is honoured across the proxy
        # chain in front of this app; without it a buffering hop collects the whole
        # stream and delivers it at once, which is the difference between "the run
        # streams" and "the page hangs for 20 seconds and then dumps everything".
        # no-cache stops an intermediary serving a second visitor the first one's run.
        headers={
            "X-Relay-Run-Uid": run_uid,
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/metrics")
async def metrics() -> dict:
    def _read() -> dict:
        payload = run_metrics(app.state.conn)
        # Composed HERE rather than inside run_metrics, so telemetry does not import
        # ratelimit — the aggregation layer has no business knowing about the
        # perimeter. And it is budget_snapshot's own arithmetic, never a second
        # derivation: the gauge renders the number that refuses the visitor's next run
        # (D-11), which includes the spend reserved by runs still in flight. A gauge
        # summing committed `runs` rows alone reads up to concurrency x max_run_cost_usd
        # low and promises budget the very next request answers 503 to.
        payload["budget"] = budget_snapshot(app.state.conn)
        return payload

    # ONE offload for both reads: two would take Database's lock twice for a route the
    # dashboard polls every 5s per open tab, which is the last place that may hold the
    # loop that also answers the 3s container HEALTHCHECK.
    return await asyncio.to_thread(_read)


@app.get("/runs/{run_uid}", dependencies=[Depends(run_detail_gate)])
async def run_detail(run_uid: str) -> dict:
    """One run's steps, redacted server-side (D-01, DASH-03).

    Public and keyless, like /events and /metrics, and safe for the same reason:
    content control, not access control. Every step below came out of
    project_run_detail's field-by-field allowlist, so holding a uid grants nothing that
    is not already redacted (D-03).

    The one exception is a DEMO-originated ticket, where the visitor authored the
    content themselves and D-02 makes the raw trace the payoff. That decision is made
    HERE, from `tickets.origin`, and is unreachable from the request: this signature
    takes no query parameter, reads no header and no cookie, and the comparison is
    `origin == "demo"` by equality — NULL (a legacy row) and anything else fail closed
    (T-06-14). `customer_email` is withheld even on the demo branch: /tickets accepts an
    arbitrary address from anyone holding the published key, so it is the one field a
    visitor could use to publish a third party's identifier. Withheld BY VALUE and not
    merely as a column (CR-01) — it used to be dropped from this envelope while
    `lookup_customer`'s raw input and raw result republished it, along with the rest of
    that person's row and ten of their ticket subjects. The demo branch now publishes
    raw tool payloads per tool (`_DEMO_RAW_TOOLS`), and the address the ticket names is
    passed to the projector as a `withheld` literal so the model's prose cannot carry it
    out either.

    This route passes ONE literal, and deliberately no longer tries to enumerate the
    rest. The address is what the model was handed in its prompt, so it can appear in
    prose even when the lookup missed and no tool result carries it; everything else the
    run looked up is derived by the projector from the run's own tool results
    (`withheld_from_run`), because a list assembled here would only ever cover the
    columns whoever last edited this function thought of. The customer's NAME, their
    PLAN and the ticket SUBJECTS beside them were exactly that gap: masked nowhere,
    restated into prose by a model this service's own prompt instructs to read them.

    None of that makes prose safe, and this docstring will not claim it does: masking is
    by literal, so a paraphrase of what the run looked up survives it. See
    `project_run_detail`.

    A run whose steps the 30-day retention swept is NOT a 404. `purge_expired_run_events`
    deliberately spares the `runs` row, so "no steps" is the normal end state of the back
    catalogue, and a visitor following a link into it has to be able to tell "this run
    happened and its detail expired" from "this uid is a lie" (T-06-18). The status
    string discloses nothing about content.
    """
    if not _RUN_UID_RE.match(run_uid):
        # Refused before the database is touched: the uid shape is known exactly, so a
        # 404 for a malformed key costs a regex where a 404 for an absent key costs a
        # query. The log carries a truncated echo only — enough to correlate a scan,
        # never enough to be a mirror for arbitrary client text.
        logger.info("run_detail.rejected_uid", extra={"ctx": {"uid": run_uid[:12]}})
        raise HTTPException(404, "unknown run")

    def _read():
        conn = app.state.conn
        # Events FIRST. Every event row is committed before the `runs` row exists (it is
        # written in event_stream's finally), so a run that finishes between these two
        # reads renders complete-with-all-its-steps rather than complete-with-a-hole.
        # The LIMIT is what keeps one run's response bounded: nothing caps a single
        # run's step count except the agent's own step limit, which is configuration.
        rows = conn.execute(
            "SELECT seq, type, payload, elapsed_ms, ticket_id FROM run_events"
            " WHERE run_uid = ? ORDER BY seq LIMIT ?",
            (run_uid, settings.run_detail_max_events),
        ).fetchall()
        run = conn.execute(
            f"SELECT {', '.join(_DETAIL_RUN_COLUMNS)} FROM runs WHERE run_uid = ?",
            (run_uid,),
        ).fetchone()
        expired = False
        if run is not None and not rows:
            # Asked of SQLite with the same expression purge_expired_run_events uses,
            # rather than re-derived from a Python clock: the two must agree about what
            # "older than the retention window" means or the page contradicts the sweep.
            expired = bool(conn.execute(
                "SELECT ? < datetime('now', ?) AS expired",
                (run["created_at"], f"-{settings.events_retention_days} days"),
            ).fetchone()["expired"])
        ticket_id = rows[0]["ticket_id"] if rows else (run["ticket_id"] if run else None)
        origin, ticket = None, None
        if ticket_id is not None:
            got = conn.execute(
                "SELECT origin FROM tickets WHERE id = ?", (ticket_id,)
            ).fetchone()
            origin = got["origin"] if got else None
            if origin == "demo":
                # Read at all ONLY on the branch that publishes it. A single SELECT
                # naming subject/body unconditionally would work identically today and
                # leave the redacted path holding the text in a local, one edit away
                # from disclosing it.
                #
                # `customer_email` is read here to be WITHHELD, never to be published:
                # it is the one value the projector needs in order to keep it out of the
                # model's prose (CR-01), and it appears nowhere in the response.
                ticket = dict(conn.execute(
                    "SELECT subject, body, customer_email FROM tickets WHERE id = ?",
                    (ticket_id,),
                ).fetchone())
        # fetchall()/fetchone() all happen in here, never after: Database materialises
        # rows while its lock is held.
        return rows, run, expired, ticket_id, origin, ticket

    rows, run, expired, ticket_id, origin, ticket = await asyncio.to_thread(_read)
    if run is None and not rows:
        raise HTTPException(404, "unknown run")

    demo = origin == "demo"
    # The one literal only THIS route knows: the address the ticket names, which
    # /tickets accepts from anyone holding the published key and which reaches the model
    # through the prompt rather than through a tool result — so it is still in the prose
    # of a run whose lookup returned {"found": false} and whose rows therefore carry it
    # nowhere. Everything the run LOOKED UP is the projector's to derive from the run's
    # own rows; adding it here would be a second, staler copy of that decision. Empty off
    # the demo branch, where nothing raw is published for a literal to hide in.
    withheld = ()
    if demo and ticket is not None and ticket["customer_email"]:
        withheld = (ticket["customer_email"],)
    note = None
    if rows and run is not None:
        status = "complete"
    elif rows:
        # The `runs` row lands in event_stream's finally, so this is what every run
        # looks like while it is still streaming.
        status = "in_flight"
    elif expired:
        status = "swept"
        note = (
            f"Step detail is kept for {settings.events_retention_days} days."
            " This run's steps have expired; its summary is kept."
        )
        logger.info("run_detail.swept", extra={"ctx": {
            "uid": run_uid[:12], "status": status,
        }})
    else:
        # A legacy pre-Phase-5 run, or one whose per-step writes failed. Nothing
        # expired, so calling it "swept" would be a lie about retention.
        status = "unrecorded"

    detail = {
        "run_uid": run_uid,
        "ticket_id": ticket_id,
        # The FLAG is not secret — the page labels "you submitted this" with it. The
        # CONTENT it gates is.
        "demo": demo,
        "status": status,
        "run": dict(run) if run is not None else None,
        "steps": project_run_detail(
            rows,
            full_fidelity=demo,
            # Each registered tool's DECLARED input keys — the schema the model was
            # shown. The projector clamps model-chosen tool names and argument keys
            # against this, so neither can carry a payload into a browser.
            known_tools={
                name: frozenset(spec.schema["input_schema"]["properties"])
                for name, spec in app.state.registry.items()
            },
            withheld=withheld,
            # The visitor's own subject. It was load-bearing while `lookup_customer`
            # handed the address's last ten SUBJECTS back — this run's own row was one
            # of them, so without this the run-derived mask withheld the visitor's words
            # from the visitor. That payload dropped `subject`, so nothing harvested
            # reaches this exemption today; it is kept for the tool that returns
            # visitor-authored text next, and events.py states plainly what it does and
            # does not protect against.
            authored=(ticket["subject"],) if demo and ticket is not None else (),
        ),
    }
    if note is not None:
        detail["note"] = note
    if demo and ticket is not None:
        # The visitor's own words back, and only these two fields by name (Q3) — through
        # the same value mask as the steps, because a visitor who typed a third party's
        # address into the body would otherwise publish it here instead.
        detail["ticket"] = {
            "subject": mask_withheld(ticket["subject"], withheld),
            "body": mask_withheld(ticket["body"], withheld),
        }
    return detail


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
            # Both serialisers this generator may use live in events.py, beside the
            # allowlist they belong to (WR-04). Nothing else may be yielded here that is
            # not an SSE comment — see test_events_output_comes_only_from_two_serialisers.
            yield snapshot_frame(app.state.runs.snapshot())
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

    # Same anti-buffering pair as /process (05-REVIEW IN-02). It matters more here: a
    # buffered live feed is indistinguishable from a dead one, and the page's own status
    # line would sit on "connecting" while frames pile up in a proxy.
    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


_TEMPLATE_PATH = Path(__file__).parent / "templates" / "dashboard.html"
# Read once at import (D-04), not per request: /dashboard is the public landing
# surface and this would otherwise be a syscall per visitor. Not in lifespan either —
# nothing here binds to a loop, and an import-time failure is the loud, early signal
# that the template did not make it into the image. The path is relative to the
# PACKAGE, not to a settings value: db_path and kb_dir are deployment-configurable
# data, whereas the page is package code and travels with the wheel.
DASHBOARD_HTML = _TEMPLATE_PATH.read_text(encoding="utf-8")


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard() -> str:
    """Serve the dashboard, substituting the demo key at request time.

    D-02 publishes the demo key here and in the README. Substituting from
    settings rather than baking a literal into DASHBOARD_HTML means the page
    can never advertise a key the service would reject. A .replace() and not an
    f-string: the inline JS is full of ${...} template literals.

    An unconfigured deployment renders a neutral placeholder — /dashboard is the
    public landing surface and must not print "None" as if it were a credential.

    The key lands in TWO parsing contexts and therefore takes TWO escapers (WR-05).
    `<code>X-API-Key: …</code>` is HTML text, where html.escape() is right. `const
    DEMO_KEY = "…"` is inside `<script>`, which is RAW TEXT: entity references are not
    decoded there, so an HTML-escaped `k&amp;y` would be the key the page actually
    sends and every Try-it submission would 401 while the block above displayed the
    correct key. json.dumps() emits a complete JS string literal — quotes, backslashes
    and control characters all handled — which is why the placeholder it replaces
    includes its own quotes. `<`, `>` and `&` are then re-encoded as \\uXXXX escapes:
    JSON leaves them bare, and a key containing `</script>` would otherwise close the
    element from inside a string literal. JS decodes \\u003c back to `<`, so the value
    the page presents is still the key byte-for-byte.
    """
    published = settings.demo_key or "(not configured)"
    js_key = (
        json.dumps(published)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )
    return (
        DASHBOARD_HTML
        .replace('"__RELAY_DEMO_KEY_JS__"', js_key)
        .replace("__RELAY_DEMO_KEY__", escape(published))
    )


# Exactly the fields the Ticket model declares, named for the same reason
# _PUBLIC_RUN_COLUMNS and _DETAIL_RUN_COLUMNS are: `tickets` just grew `origin`, which is
# deliberately NOT part of the API model, and a star select would have pushed it straight
# into Ticket(**dict(row)) the moment the migration ran. What a route publishes is a list
# someone wrote down, never whatever the table happens to hold today.
_TICKET_COLUMNS = (
    "id", "customer_email", "subject", "body", "status", "category", "created_at",
)


async def _get_ticket(ticket_id: int) -> Ticket:
    # fetchone() is called inside the offloaded callable, not after it: Database
    # materialises rows while its lock is held, and stepping the result back on the
    # event loop would put the read half a statement outside the thread it belongs to.
    row = await asyncio.to_thread(
        lambda: app.state.conn.execute(
            f"SELECT {', '.join(_TICKET_COLUMNS)} FROM tickets WHERE id = ?", (ticket_id,)
        ).fetchone()
    )
    if row is None:
        raise HTTPException(404, "ticket not found")
    return Ticket(**dict(row))
