---
title: Where this stands: résumé value and next steps
level: medium
time: 25 min
summary: An honest assessment of what this project proves, how to describe it truthfully, what is missing, and which additions would raise its value the most.
tags: résumé, portfolio, interview, roadmap, honesty
---

You asked how far this project goes on a résumé. This lesson gives a direct answer, including the uncomfortable parts, because an assessment that only flatters is useless to you.

## The short verdict

:::note A strong junior-to-mid portfolio project, with specific, fixable gaps
Streambot started as a chatbot that streams tokens. It now has **accounts, a real database with migrations, web search, and retrieval over per-chat documents**, which puts it well above the usual "chatbot that calls the OpenAI API". It demonstrates engineering judgement, not just API usage. It is *not* yet a deployed, evaluated product, and the gaps below are the first things a sharp interviewer will probe.
:::

## What it genuinely proves

| Skill | Evidence in the project |
|---|---|
| Streaming systems | SSE end to end, correct cancellation, partial persistence, crash recovery |
| Failure-aware design | retry rules that never duplicate text, safe errors, fail-fast config, state machines for replies and for document indexing |
| Agents and tool use | a hand-written agent loop with two tools, numbered citations shared across web and documents, reasoning carried across rounds |
| **Retrieval (RAG)** | safe PDF/Word/text/Markdown extraction, paragraph-aware chunking with overlap, embeddings in **pgvector**, **hybrid** vector + keyword retrieval merged with Reciprocal Rank Fusion, scoped per user and per chat, a small gold-set test (hit@k, MRR) |
| **Authentication and multi-tenancy** | JWT login, argon2id hashes, vague errors and equal timing, every query filtered by owner, a test that walks every route to prove it needs a token |
| Memory design | rolling summaries with hysteresis, background tasks, merge prompts, a safety net |
| Cost engineering | exact `Decimal` accounting, honest unknowns, prompt caching with a **measured** saving, a usage dashboard that includes document indexing |
| Backend craft | layered architecture with walls, PostgreSQL with constraints and cascades, **versioned migrations** (transactional, advisory lock), connection pooling, rate limiting per user, layered upload limits |
| Frontend craft | a hand-written SSE parser, optimistic UI, cancellation, a login state machine, drag-and-drop attachments with status polling, safe rendering of untrusted output |
| Testing | over 440 backend tests (real PostgreSQL per test) and 70 frontend tests, test doubles, a network-blocking safety net, a live-server test |
| Security thinking | prompt injection (including through documents), XSS, safe links, upload hardening, secrets, privacy in logs |
| Communication | this guide: explaining a system clearly is itself a hiring signal |

The strongest single feature is that it contains **real decisions with reasons**: why never retry after the first token, why create the row before the model call, why hybrid search and not vectors alone, why 404 and not 403, why chunks and the `ready` flag are written in one transaction. Those are the answers interviewers listen for.

## Where it stands, candidly

| Role | How competitive this project is |
|---|---|
| Internship, fresher or junior **GenAI / AI engineer / full-stack with LLMs** | **Strong.** It is the kind of project that gets a second look and a longer interview. |
| Mid-level AI engineer | **Promising but incomplete.** Expect hard questions about evaluation, deployment and how you would run it in production. |
| ML research, data science, computer vision | **Mostly irrelevant** to those roles. |

Rough scores, to make the point rather than to be precise: it is a strong portfolio project today, and the gaps that separate "strong" from "excellent" are the ones below: deployment and *measured quality*.

## The gaps an interviewer will find

1. **It is not deployed.** There is no live URL and no CI. Docker Compose runs only the database. A link a recruiter can click is worth more than any bullet point.
2. **No answer-quality evaluation, and retrieval was not measured with the real embedding model.** The tests prove the plumbing, and the retrieval gold set is small and runs on a stand-in embedder. "How do you know it gives good answers?" still has no strong reply.
3. **The real embedding API has not been exercised end to end yet.** Everything around it is tested with stand-ins; a single real indexing-and-question run (cheap) is the next verification to do before you describe RAG as "working".
4. **Basic retrieval.** No re-ranker, no OCR for scanned PDFs, four file types, indexing happens inside the web process (a restart interrupts it).
5. **Basic auth.** A stateless 7-day JWT cannot be revoked early, it lives in `localStorage`, and there is no email verification or password reset.
6. **One provider.** The `LLM` protocol makes adding another easy, but it is not shown.
7. **An uncapped agent loop** is a cost risk in public use (a deliberate choice, but worth defending).

## A truthful way to describe it

Strong bullets are **specific and measured**. These are drafts; check every number against the repository before you use it.

- Built **Streambot**, a full-stack ChatGPT-style app (FastAPI, OpenAI Responses API, SSE, PostgreSQL, React/TypeScript) with live token streaming, a Stop button that cancels the model call, and crash recovery that preserves partial replies.
- Added **JWT authentication** (argon2id, 7-day tokens) and **per-user data isolation**: every query is owner-scoped, other users' chats answer 404, and a test enumerates the app's routes to prove each one requires a token.
- Built **per-chat document question answering (RAG)** on **PostgreSQL + pgvector**: safe PDF/Word/text/Markdown ingestion, overlapping chunks, hybrid retrieval (HNSW vector search + full-text, merged with Reciprocal Rank Fusion) exposed to the model as a tool, with file-and-page citations.
- Implemented a **tool-calling agent loop** with web search and numbered citations; designed retries so a failed request **never repeats a search or duplicates streamed text**.
- Designed **rolling-summary memory** (hysteresis thresholds, background task, merge prompt) so long chats keep their context while the prompt prefix stays stable.
- Structured prompts for **OpenAI prompt caching**, verified live (2,439 of 2,442 input tokens cached; about **86% cheaper** on the repeat call), and built a **usage dashboard** (spend, cache hit rate, savings) with exact `Decimal` cost accounting in USD and INR.
- Wrote **over 440 backend and 70 frontend tests** against a real PostgreSQL, a scripted model, a fake embedder and a network-blocking safety net; none of the tests call external APIs.

### Do not claim

- **"Production-ready"** or "scales to N users": it is a single-process app that has never been load tested or deployed.
- **Latency, retrieval-accuracy or answer-accuracy numbers** you have not measured properly. The first-token times seen were single samples. The retrieval gold set (hybrid MRR 1.0 against 0.875 for vectors alone) is a plumbing check on a stand-in embedder over three tiny documents: **do not present it as RAG accuracy**.
- **That the embedding integration is verified live**, until you have run it once against the real API.
- **Evals, multi-provider routing, deployment**: none of these exist yet.
- That you wrote it **"alone from scratch"** if AI tools helped you build it. Describe it accurately, for example: *"designed and built with AI-assisted development; I can explain and modify every part."* That last clause must be true.

:::warn The interview test that matters
Interviewers probe depth. They may ask you to change something live: add a header, explain a line, find why a test would fail, or re-implement a small piece. If you cannot, the project hurts you. This guide exists so that you *can*. A practical drill: re-implement `build_history`, the SSE parser or the RRF merge from a blank file, without looking, and compare. If you can do that and explain the reasons, you own the project.
:::

## Three additions that raise its value the most

In priority order, each builds on what exists:

1. **Deploy it.** A Dockerfile for the whole app, GitHub Actions running the tests, and a public demo URL (with a spend cap and rate limit, since it costs real money). This converts "a repository" into "a product".
2. **Measure quality.** First, run the retrieval gold set with the *real* embedder on real documents of your own (50+ questions, including ones the documents cannot answer), and report hit@k and MRR honestly. Then score whole *answers* with exact checks and a judge model, across prompts and models, reporting accuracy, cost and latency. This answers "how do you know it is good?" and gives you measured numbers you can defend.
3. **Harden what exists.** Refresh tokens with revocation and an `httpOnly` cookie; a re-ranking step and OCR for retrieval; indexing in a worker queue; a spend ceiling for the agent loop; a second model provider behind the `LLM` protocol.

:::keypoints
- Streambot is **strong for junior to mid roles** in GenAI and full-stack, because it shows judgement: failure modes, cost, security, retrieval and tests, not just an API call.
- Its weak spots are **no deployment, no answer-quality evals, and an embedding integration not yet verified live**. Say so before an interviewer does.
- Write **specific, measured** bullets, and never claim what you have not built or measured. A stand-in test is not an accuracy number.
- Be able to **explain and change** any file live; practise by re-implementing small pieces from scratch.
- Best next steps, in order: **deploy**, **measure quality**, **harden**.
:::

```quiz
? Which résumé claim is safest?
- "Production-ready chatbot that scales to thousands of users."
+ "Prompt caching verified live: 2,439 of 2,442 tokens cached on the repeat call, about 86% cheaper."
- "RAG with 100% retrieval accuracy."
> Specific, measured and verifiable beats impressive and unproven. The retrieval test uses a stand-in embedder on tiny documents, so no accuracy claim is supportable.

? Which addition would most increase the project's value?
- A fourth colour theme.
+ Deploying it with CI and a public demo, then measuring retrieval and answer quality with real data.
- Renaming the repository.
> A running product and measured quality are what interviewers look for next.

? Your retrieval test scores MRR 1.0 on 12 questions. What can you honestly say?
- "My RAG system is 100% accurate."
+ "A small gold set passes on a stand-in embedder, so the chunking, fusion query and scoping work; I have not measured quality with the real embedding model on real documents yet."
- Nothing; test scores should never be mentioned.
> State what the number proves, and what it does not.
```
