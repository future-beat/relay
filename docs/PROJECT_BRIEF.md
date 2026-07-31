# Project Brief: Relay — a Production-Grade Agentic Service

## Why this project (gap analysis)

Based on the resume and current agentic AI job listings in Australia (Endava "Agentic
Developer" Perth, AWS ProServe "Agentic AI FullStack SDE" Perth, UST "AI Engineer –
Agentic AI" Sydney, July 2026), the resume is strong on **agent logic** and weak on
**production engineering around agents**.

**Already covered by the resume** (don't repeat):
- Multi-agent orchestration (LangGraph), RAG, vector search, embeddings
- Local/private LLMs (Ollama), Claude API
- Research-style projects that run on a developer's machine

**Missing — and asked for by name in nearly every listing:**
1. **Production backend service**: FastAPI, async Python, SSE streaming of agent output
2. **Deployment**: Docker, cloud hosting, a *live URL* anyone can hit
3. **CI/CD**: GitHub Actions running tests + evals on every push
4. **Evaluation framework**: automated eval suite for agent quality, regression-tested
5. **Observability**: structured logging, tracing (OpenTelemetry), token/cost/latency metrics
6. **MCP (Model Context Protocol)**: named explicitly in the AWS listing ("emerging
   protocols e.g. MCP, A2A"); absent from the resume
7. **Enterprise integration**: agent acting on a real database and REST APIs, not just
   web search
8. **Guardrails**: input/output validation, tool-permission boundaries, cost caps

Every current project on the resume is "runs locally." The gap is the full delivery
lifecycle: **build → test → deploy → monitor → evaluate**.

## The project

**Relay** — an AI support-operations agent, shipped as a production service.

A customer-support triage agent for a fictional SaaS product. It receives support
tickets via a REST API, and autonomously: classifies and prioritises the ticket,
looks up the customer and their history in a real database (Postgres/SQLite),
searches a product-docs knowledge base (RAG — reuses your existing strength),
drafts a grounded reply, and either resolves the ticket or escalates it with a
structured handover — streaming its reasoning steps to the client via SSE.

The domain is deliberately boring and enterprise-shaped: that is what Endava, AWS
and UST are hiring for.

## Scope (in order — each phase is shippable)

1. **Core agent service** — FastAPI + async Python; agent loop with tool use
   (Claude API); tools: `lookup_customer`, `search_docs`, `create_escalation`,
   `send_reply` (mocked email); SSE streaming of agent steps; SQLite + seeded data.
2. **Guardrails & reliability** — Pydantic-validated tool I/O, per-request cost/step
   caps, tool permission tiers (read vs. write), graceful failure + retry.
3. **Evaluation harness** — a golden dataset of ~50 tickets with expected outcomes;
   automated eval run (correct classification, grounded answers via LLM-as-judge,
   no hallucinated policies); a score report artifact.
4. **Observability** — structured JSON logs, OpenTelemetry traces per agent run,
   token/cost/latency metrics endpoint; a simple `/dashboard` page.
5. **MCP server** — expose the same tools as an MCP server so any MCP client
   (Claude Desktop/Code) can drive the system; documents the protocol skill.
6. **Ship it** — Dockerfile + docker-compose; GitHub Actions (lint, tests, evals on
   PR); deploy to a free tier (Fly.io / Railway / Render) with a live demo URL and
   a minimal web UI to submit a ticket and watch the agent stream.

## Stack

Python 3.12, FastAPI, Pydantic, Claude API (agent loop written by hand — no
LangGraph this time, to show you understand the loop itself), SQLite → Postgres,
your existing embeddings experience for RAG, OpenTelemetry, pytest, Docker,
GitHub Actions, MCP Python SDK.

## Resume bullets this earns (draft)

- Built and deployed **Relay**, a production-grade support-triage agent that reads
  tickets, queries a customer database, searches product docs and resolves or
  escalates autonomously — live at <demo URL>
- Shipped the full delivery lifecycle: FastAPI service with SSE streaming, Docker,
  CI/CD via GitHub Actions, and cloud deployment
- Built an automated evaluation harness (golden dataset + LLM-as-judge) that runs
  in CI, catching agent-quality regressions before deploy
- Instrumented the agent with OpenTelemetry tracing and per-run token/cost/latency
  metrics
- Exposed the agent's tools over the Model Context Protocol (MCP), enabling any
  MCP client to drive the system

## Success criteria

- Public GitHub repo with README, architecture diagram, and eval results table
- Live demo URL that a recruiter can click
- Green CI badge; evals run on every PR
- A short write-up (you've done this before for Baynetna) on designing the eval
  harness — the least-commoditised skill on the list
