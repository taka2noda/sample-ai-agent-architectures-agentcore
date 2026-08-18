# CLAUDE.md

## Repository Overview

This repo demonstrates progressively hardened architectures for deploying AI agents on AWS using **Amazon Bedrock AgentCore Runtime**. All agents are LangGraph ReAct agents (`langgraph.prebuilt.create_react_agent` + `langchain_aws.ChatBedrockConverse`) wrapped in `bedrock_agentcore.runtime.BedrockAgentCoreApp`. The agent logic itself is intentionally trivial (a couple of tools: current time, US weather lookup) — the point of each iteration is the surrounding infra.

Four iterations, each in its own top-level directory, each independently deployable:

| Iteration | Stack | Auth on agent |
|---|---|---|
| `iteration-0/` | Browser (static `frontend/index.html`) → AgentCore Runtime directly | OAuth (Cognito JWT) |
| `iteration-1/` | Browser → API Gateway (+WAF) → AgentCore Runtime | OAuth pass-through (Cognito JWT) |
| `iteration-2/` | Browser → API Gateway → Lambda (`lambda/app.py`) → AgentCore Runtime | IAM (Lambda's role) |
| `iteration-3/` | Browser → API Gateway → Lambda (`functions/chat`, `functions/conversations`) → AgentCore Runtime + AgentCore Memory + DynamoDB | IAM, plus conversation persistence |

Shared prerequisite: `iteration-0/cognito.yaml` (Cognito User Pool + IAM execution role) is deployed once and reused by all iterations.

Deployment tooling: `agentcore` CLI (`pip install bedrock-agentcore`) for the agent itself, AWS SAM CLI for Lambda/API Gateway stacks (iterations 2–3), raw CloudFormation for iteration-0/1's Cognito/API Gateway/WAF.

Full setup steps for each iteration are in that iteration's `README.md` — follow those verbatim for deploy/config prompts (agent name, execution role, OAuth vs IAM, memory config, etc). Don't improvise flags that diverge from the documented `agentcore configure` prompts.

## Working Style For This Repo

The user works through this repo **iteration by iteration**, and for each iteration the loop is:

1. **App implementation** — get that iteration's agent + frontend + infra deployed and working end-to-end per its README (Cognito → agent → API layer → frontend).
2. **Datadog instrumentation** — once the app works, add Datadog observability for that iteration:
   - **RUM** — instrument the static `frontend/index.html` (browser SDK snippet) to trace user sessions/actions calling into the API/agent.
   - **APM** — instrument the server-side compute for that iteration (Lambda handlers in iterations 2–3, and/or the AgentCore Runtime agent process itself) so requests trace end-to-end.
   - **Agent Observability** — instrument the LangGraph/LangChain agent invocation (`agent.py`) so LLM calls, tool calls, and reasoning steps show up as agent traces in Datadog, correlated with the APM trace.

Do this as a repeatable pattern per iteration: don't jump ahead to instrumenting iteration N+1 until the current iteration's app + Datadog setup both work. Each iteration inherits the previous iteration's Datadog approach where the architecture is unchanged (e.g. iteration-3's chat Lambda instrumentation should look like iteration-2's, plus the new conversations Lambda).

Datadog concerns to work out per iteration (ask the user for account/site specifics if not already known — API key, DD site, service naming convention):
- Lambda: use the Datadog Lambda extension/layer for APM+trace propagation (iterations 2–3).
- AgentCore Runtime agent process: since it's a standalone Python process (not Lambda), APM instrumentation is via `ddtrace` (dd-trace-py) — check whether AgentCore's runtime constraints (packaging, cold start, network egress) affect how the tracer/agent reaches the Datadog Agent or intake.
- LLM/agent tracing: use Datadog's LLM Observability / Agent Observability SDK integration for LangChain/LangGraph (`ddtrace` LLMObs or equivalent) to capture prompts, tool calls, and model latency.
- RUM: browser SDK init in `frontend/index.html`, with RUM-to-APM trace correlation (allowed tracing origins matching the API Gateway/agent endpoints) so a browser action can be traced through to the backend and LLM spans.
- Service naming/tagging should reflect the iteration (e.g. `agentcore-sample-iteration-2-chat-lambda`) so iterations don't collide in the same Datadog org.

## Environment Notes

- Docker/Colima: if any Datadog Agent container or local testing needs Docker, use **Colima**, not Docker Desktop (`colima start`, `colima status`, `colima stop`).
- AWS CLI must be configured (`aws sts get-caller-identity` to verify) and Bedrock model access enabled for Claude models in the target account/region (default `us-east-1` in the READMEs).
- Python 3.11+ required for agent code.
