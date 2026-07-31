import json
from contextlib import asynccontextmanager

from anthropic import AsyncAnthropic
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse

from . import __version__
from .agent import run_ticket
from .config import settings
from .db import connect, init_db
from .models import Ticket, TicketCreate
from .tools import build_registry


@asynccontextmanager
async def lifespan(app: FastAPI):
    conn = connect(settings.db_path)
    init_db(conn)
    app.state.conn = conn
    app.state.registry = build_registry(conn, settings.kb_dir)
    app.state.client = AsyncAnthropic()
    yield
    conn.close()


app = FastAPI(title="Relay", version=__version__, lifespan=lifespan)


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
async def process_ticket(ticket_id: int) -> StreamingResponse:
    """Run the agent on a ticket, streaming each step as a server-sent event."""
    ticket = _get_ticket(ticket_id)
    if ticket.status != "open":
        raise HTTPException(409, f"ticket is already {ticket.status.value}")

    async def event_stream():
        async for event in run_ticket(
            app.state.client, app.state.registry, ticket.model_dump()
        ):
            yield f"event: {event.type}\ndata: {json.dumps(event.data)}\n\n"
        yield "event: done\ndata: {}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


def _get_ticket(ticket_id: int) -> Ticket:
    row = app.state.conn.execute(
        "SELECT * FROM tickets WHERE id = ?", (ticket_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(404, "ticket not found")
    return Ticket(**dict(row))
