import json
import time
from contextlib import asynccontextmanager

from anthropic import AsyncAnthropic
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse

from . import __version__
from .agent import run_ticket
from .config import settings
from .db import connect, init_db
from .guardrails import ToolPolicy
from .models import Ticket, TicketCreate
from .telemetry import configure_logging, record_run, run_metrics, setup_tracing
from .tools import build_registry


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    setup_tracing()
    conn = connect(settings.db_path)
    init_db(conn)
    app.state.conn = conn
    app.state.registry = build_registry(conn, settings.kb_dir)
    app.state.client = AsyncAnthropic(api_key=settings.anthropic_api_key)
    yield
    conn.close()


app = FastAPI(title="Relay", version=__version__, lifespan=lifespan)


@app.get("/", include_in_schema=False)
async def root() -> RedirectResponse:
    """Send visitors to the dashboard rather than a bare 404."""
    return RedirectResponse("/dashboard")


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "version": __version__, "model": settings.model}


@app.post("/tickets", response_model=Ticket, status_code=201)
async def create_ticket(payload: TicketCreate) -> Ticket:
    conn = app.state.conn
    cur = conn.execute(
        "INSERT INTO tickets (customer_email, subject, body) VALUES (?, ?, ?)",
        (payload.customer_email, payload.subject, payload.body),
    )
    conn.commit()
    return _get_ticket(cur.lastrowid)


@app.get("/tickets/{ticket_id}", response_model=Ticket)
async def get_ticket(ticket_id: int) -> Ticket:
    return _get_ticket(ticket_id)


@app.post("/tickets/{ticket_id}/process")
async def process_ticket(ticket_id: int, dry_run: bool = False) -> StreamingResponse:
    """Run the agent on a ticket, streaming each step as a server-sent event.

    With dry_run=true, write-tier tools are denied by policy: the agent can
    read data and search docs but cannot reply, escalate, or categorise.
    Every run is recorded in the runs table for /metrics.
    """
    ticket = _get_ticket(ticket_id)
    if ticket.status != "open":
        raise HTTPException(409, f"ticket is already {ticket.status.value}")

    async def event_stream():
        started = time.perf_counter()
        usage: dict = {}
        outcome = "incomplete"
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
        yield "event: done\ndata: {}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.get("/metrics")
async def metrics() -> dict:
    return run_metrics(app.state.conn)


DASHBOARD_HTML = """<!doctype html>
<html><head><title>Relay dashboard</title><style>
body { font-family: ui-monospace, monospace; max-width: 900px; margin: 2rem auto; padding: 0 1rem; }
h1 { font-size: 1.2rem; } .cards { display: flex; gap: 1rem; flex-wrap: wrap; }
.card { border: 1px solid #ccc; border-radius: 8px; padding: .8rem 1.2rem; min-width: 140px; }
.card b { display: block; font-size: 1.4rem; } .card span { color: #666; font-size: .8rem; }
table { border-collapse: collapse; width: 100%; margin-top: 1.5rem; font-size: .85rem; }
th, td { text-align: left; padding: .3rem .6rem; border-bottom: 1px solid #eee; }
</style></head><body>
<h1>Relay — agent runs</h1><div class="cards" id="cards"></div>
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
    return DASHBOARD_HTML


def _get_ticket(ticket_id: int) -> Ticket:
    row = app.state.conn.execute(
        "SELECT * FROM tickets WHERE id = ?", (ticket_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(404, "ticket not found")
    return Ticket(**dict(row))
