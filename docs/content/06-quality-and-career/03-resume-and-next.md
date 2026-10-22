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
As it stands, Streambot is **well above the usual "chatbot that calls the OpenAI API"** that most candidates show. It demonstrates engineering judgement, not just API usage. It is *not* yet a deployed, multi-user, evaluated product, and the gaps below are the first things a sharp interviewer will probe.
:::

## What it genuinely proves

| Skill | Evidence in the project |
|---|---|
| Streaming systems | SSE end to end, correct cancellation, partial persistence, crash recovery |
| Failure-aware design | retry rules that never duplicate text, safe errors, fail-fast config, a state machine for replies |
| Agents and tool use | a hand-written agent loop, web search, numbered citations, reasoning carried across rounds |
| Memory design | rolling summaries with hysteresis, background tasks, merge prompts, a safety net |
| Cost engineering | exact `Decimal` accounting, honest unknowns, prompt caching with a **measured** saving, a usage dashboard |
| Backend craft | layered architecture with walls, schema constraints, additive migrations, rate limiting, input caps |
| Frontend craft | a hand-written SSE parser, optimistic UI, cancellation, safe rendering of untrusted output |
| Testing | 262 backend and 38 frontend tests, test doubles, a network-blocking safety net, a live-server test |
| Security thinking | prompt injection, XSS, safe links, secrets, privacy in logs |
| Communication | this guide: explaining a system clearly is itself a hiring signal |

The strongest single feature is that it contains **real decisions with reasons**: why never retry after the first token, why create the row before the model call, why a summary boundary should move rarely. Those are the answers interviewers listen for.

## Where it stands, candidly

| Role | How competitive this project is |
|---|---|
| Internship, fresher or junior **GenAI / AI engineer / full-stack with LLMs** | **Strong.** It is the kind of project that gets a second look and a longer interview. |
| Mid-level AI engineer | **Promising but incomplete.** Expect questions about deployment, evaluation, RAG, multi-user concerns. |
| ML research, data science, computer vision | **Mostly irrelevant** to those roles. |

Rough scores, to make the point rather than to be precise: **about 7 out of 10 today**, among portfolio chatbots; about **8.5 out of 10** after the three additions at the end of this lesson.

## The gaps an interviewer will find

1. **It is not deployed.** There is no live URL, no Docker setup, no CI. A link a recruiter can click is worth more than any bullet point.
2. **No evaluation.** The tests prove the plumbing, not the quality of answers. "How do you know it gives good answers?" has no strong reply yet.
3. **No retrieval (RAG).** It is the most requested GenAI skill, and this project does not show it.
4. **Single user, no authentication.** Anyone who can reach the API sees every chat.
5. **One provider.** The `LLM` protocol makes adding another easy, but it is not shown.
6. **An uncapped agent loop** is a cost risk in public use (a deliberate choice, but worth defending).

## A truthful way to describe it

Strong bullets are **specific and measured**. These are drafts; check every number against the repository before you use it.

- Built **Streambot**, a full-stack ChatGPT-style app (FastAPI, OpenAI Responses API, SSE, SQLite, React/TypeScript) with live token streaming, a Stop button that cancels the model call, and crash recovery that preserves partial replies.
- Implemented a **tool-calling agent loop** with Tavily web search and numbered source citations; designed retries so a failed request **never repeats a search or duplicates streamed text**.
- Designed **rolling-summary memory** (hysteresis thresholds, background task, merge prompt) so long chats keep their context while the prompt prefix stays stable.
- Structured prompts for **OpenAI prompt caching**, verified live (2,439 of 2,442 input tokens cached; about **86% cheaper** on the repeat call), and built a **usage dashboard** (spend, cache hit rate, savings) with exact `Decimal` cost accounting in USD and INR.
- Wrote **262 backend and 38 frontend tests** using a scripted model, an SDK stub and a network-blocking safety net, plus a live-server test for client disconnects; none of the tests call external APIs.

### Do not claim

- **"Production-ready"** or "scales to N users": it is a single-process app with SQLite.
- **Latency or accuracy numbers** you have not measured properly (the first-token times seen were single samples, not a benchmark).
- **RAG, evals, multi-provider routing, authentication, deployment**: none of these exist yet.
- That you wrote it **"alone from scratch"** if AI tools helped you build it. Describe it accurately, for example: *"designed and built with AI-assisted development; I can explain and modify every part."* That last clause must be true.

:::warn The interview test that matters
Interviewers probe depth. They may ask you to change something live: add a header, explain a line, find why a test would fail, or re-implement a small piece. If you cannot, the project hurts you. This guide exists so that you *can*. A practical drill: re-implement `build_history` or the SSE parser from a blank file, without looking, and compare. If you can do that and explain the reasons, you own the project.
:::

## Three additions that raise its value the most

In priority order, each builds on what exists:

1. **Deploy it.** A Dockerfile and `docker compose up`, GitHub Actions running the tests, and a public demo URL (with a spend cap and rate limit, since it costs real money). This converts "a repository" into "a product".
2. **Add retrieval (RAG)** over uploaded documents with page-level citations. You already have the hard parts: streaming, citations, tool calling and cost tracking. A document-search tool is one more entry in the tool registry.
3. **Add an evaluation harness**: a fixed set of questions, scored by exact checks and a judge model, run against different prompts and models, reporting accuracy, cost and latency. This answers "how do you know it is good?" and gives you honest, measured numbers.

After those: authentication and per-user data, a second model provider behind the `LLM` protocol, and a spend ceiling for the agent loop.

:::keypoints
- Streambot is **strong for junior to mid roles** in GenAI and full-stack, because it shows judgement: failure modes, cost, security and tests, not just an API call.
- Its weak spots are **no deployment, no evals, no RAG, no auth**. Say so before an interviewer does.
- Write **specific, measured** bullets, and never claim what you have not built or measured.
- Be able to **explain and change** any file live; practise by re-implementing small pieces from scratch.
- Best next steps, in order: **deploy**, **RAG**, **evals**.
:::

```quiz
? Which résumé claim is safest?
- "Production-ready chatbot that scales to thousands of users."
+ "Prompt caching verified live: 2,439 of 2,442 tokens cached on the repeat call, about 86% cheaper."
- "Achieves 95% answer accuracy."
> Specific, measured and verifiable beats impressive and unproven. There is no evaluation, so no accuracy claim is supportable.

? Which addition would most increase the project's value?
- A fourth colour theme.
+ Deploying it with CI and a public demo, then adding RAG and an evaluation harness.
- Renaming the repository.
> A running product, the most requested GenAI skill, and measured quality are what interviewers look for next.
```
