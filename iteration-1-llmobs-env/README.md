# Iteration 1 (LLMObs env-only variant): enabling ddtrace/LLM Observability without touching agent.py

This is a copy of [iteration-1](../iteration-1/) (Browser → Amazon API Gateway → Amazon Bedrock AgentCore Runtime, OAuth pass-through) used to answer a specific question: **can Datadog APM + LLM Observability be turned on for the AgentCore agent process without adding any `ddtrace`/`LLMObs` code to `agent.py`?**

It does not touch iteration-1's own deployed agent (`agent_1`) — this uses a separate agent name (`agent_1_llmobs_env`) so both can coexist. **This has been deployed and verified end-to-end** (see "What we verified" below).

## TL;DR answer

**Yes.** `agent/agent.py` in this directory is byte-for-byte the plain, non-instrumented agent — no `import ddtrace`, no `LLMObs.enable(...)` call. Enabling is done entirely via:

1. A `sitecustomize.py` file dropped next to `agent.py` containing one line: `import ddtrace.auto`.
2. One extra environment variable, `PYTHONPATH=.`, plus the usual `DD_*` config vars — no code, no Dockerfile.

## Two approaches that looked promising but don't work here

This iteration uses `deployment_type: direct_code_deploy` (per iteration-1's README) — AgentCore zips the source and runs it directly on a managed runtime, **there is no Dockerfile/container involved at all**. That ruled out two ideas before landing on the one that works:

1. **Prepend `ddtrace-run` to the container's Dockerfile `CMD`.** This only applies to `deployment_type: container`. For `direct_code_deploy`, the toolkit builds an `entryPoint` array (e.g. `["agent.py"]`, or `["opentelemetry-instrument", "agent.py"]` when AWS's own OTel observability is enabled) that's passed directly to the `CreateAgentRuntime`/`UpdateAgentRuntime` API's `agentRuntimeArtifact.codeConfiguration.entryPoint` field — there's no Dockerfile to edit. (Confirmed this *does* work for `deployment_type: container` — see [`../iteration-1-container-ddtrace-run/`](../iteration-1-container-ddtrace-run/), a separate deployed-and-verified variant that tests exactly this.)
2. **Call `UpdateAgentRuntime` directly (bypassing the `agentcore` CLI) to set `entryPoint: ["ddtrace-run", "agent.py"]`.** This field is a plain API parameter, so it seemed like it should work the same way `["opentelemetry-instrument", "agent.py"]` does (which we confirmed *is* accepted). It isn't: the API rejects any wrapper other than `opentelemetry-instrument` with `ValidationException: Invalid entrypoint value...`, i.e. that first token appears to be allow-listed rather than genuinely arbitrary. Confirmed by testing directly against the deployed `agent_1_llmobs_env` runtime via boto3.

## What actually works: `sitecustomize.py` + `PYTHONPATH`

CPython's `site` module auto-imports a module named `sitecustomize` at interpreter startup if one is found on `sys.path` — but only if it's reachable via `PYTHONPATH`/`site-packages`, **not** merely by sitting in the script's own directory (verified empirically: a `sitecustomize.py` next to a script, run as `python script.py` with no `PYTHONPATH`, is never imported; the same file with `PYTHONPATH=.` set *is* imported).

So:
- `agent/sitecustomize.py` — the only new file, one line: `import ddtrace.auto  # noqa: F401`. `ddtrace.auto` runs the exact same bootstrap (`ddtrace/bootstrap/sitecustomize.py` → `ddtrace/bootstrap/preload.py`) as `ddtrace-run` does, including auto-starting the LLMObs "product" (`ddtrace/llmobs/_product.py`) when `DD_LLMOBS_ENABLED=1` is set — it isn't limited to APM tracing.
- `PYTHONPATH=.` — passed as an `--env` var at deploy time. Since `entryPoint` is the relative path `agent.py` and the managed runtime's cwd is the code root, `.` resolves to that same directory, making `sitecustomize.py` importable.

`agent.py` itself needed zero changes — it's identical to a version of the agent with no Datadog instrumentation whatsoever.

## Environment variables (replaces the old `LLMObs.enable(...)` kwargs)

```bash
agentcore deploy \
  --env "DD_API_KEY=${DD_API_KEY}" \
  --env "DD_SITE=datadoghq.com" \
  --env "DD_ENV=sandbox" \
  --env "DD_SERVICE=agentcore-iteration-1-llmobs-env-agent" \
  --env "DD_LLMOBS_ENABLED=1" \
  --env "DD_LLMOBS_ML_APP=agentcore-iteration-1-llmobs-env-agent" \
  --env "DD_LLMOBS_AGENTLESS_ENABLED=1" \
  --env "DD_TRACE_LANGCHAIN_ENABLED=false" \
  --env "PYTHONPATH=." \
  --env 'DD_TRACE_SAMPLING_RULES=[{"resource": "GET /ping", "sample_rate": 0}]'
```

Mapping from the old code-based call:

| `LLMObs.enable(...)` kwarg | Equivalent env var |
|---|---|
| `ml_app=...` | `DD_LLMOBS_ML_APP` |
| `api_key=...` | `DD_API_KEY` |
| `site=...` | `DD_SITE` |
| `agentless_enabled=True` | `DD_LLMOBS_AGENTLESS_ENABLED=1` |
| *(the call itself)* | `DD_LLMOBS_ENABLED=1` + `sitecustomize.py` (`import ddtrace.auto`) + `PYTHONPATH=.` |

`DD_TRACE_LANGCHAIN_ENABLED=false` is still required — same `ddtrace`+LangGraph crash workaround as iteration-1 (see the root README's "Known issues / gotchas"). `DD_TRACE_SAMPLING_RULES` drops AgentCore's own `GET /ping` health-check noise from APM, same as iteration-1.

## What we verified

Deployed `agent_1_llmobs_env` to the shared sandbox account/region (us-west-2, same shared Cognito stack as iteration-1) with the env vars above, then invoked it with a real Cognito JWT (`agentcore invoke --bearer-token ...`):

- **CloudWatch logs** for the invocation show `ddtrace/llmobs/_integrations/langgraph.py` actively instrumenting the LangGraph call (a `LangChainDeprecationWarning` raised *from inside* that ddtrace file is proof the integration patched LangChain's `BaseChatModel`) — despite `agent.py` never importing `ddtrace`.
- **Datadog APM** shows the full trace for the invocation: a `starlette.request` root span (`POST /invocations`) plus LLM Observability spans `langgraph.graph.state.CompiledStateGraph.LangGraph` → `RunnableSeq.call_model` → `RunnableSeq.tools` → `RunnableSeq.call_model`, tagged `service:agentcore-iteration-1-llmobs-env-agent`, `env:sandbox`, `ingestion_reason:auto`, with `llmobs_trace_id`/`llmobs_parent_id` correlating the LLM Observability spans to the APM trace.

This confirms the approach works end-to-end, not just in theory.

## Setup

Follows the same deployment shape as [iteration-1](../iteration-1/README.md) (shared Cognito stack, OAuth pass-through, API Gateway + WAF), with these differences:

1. **Prerequisites**: same as iteration-1 (shared `agentcore-cognito` stack, test user) — see that iteration's README for those steps.
2. **Configure and deploy the agent** — same `agentcore configure` flow as iteration-1 (`direct_code_deploy`, `PYTHON_3_11`, OAuth authorizer pointed at the same Cognito pool), but use a distinct agent name (`agent_1_llmobs_env`) so you don't collide with an already-deployed `agent_1`. `agent/sitecustomize.py` is packaged automatically since it's just another file in the source directory.
3. **Deploy** with `agentcore deploy` and the env vars listed above (including `PYTHONPATH=.`) — no Dockerfile step, no entrypoint override needed.
4. **API Gateway + frontend**: identical to iteration-1 — deploy `api-gateway.yaml` with the new agent's Runtime ID, update `frontend/index.html`'s `CONFIG`. RUM instrumentation on `frontend/index.html` is unchanged/untouched by this experiment — it's only exploring the agent-process side. (Not deployed as part of this validation pass — verification above used `agentcore invoke` directly against the Runtime with a Cognito JWT, which is sufficient to prove the mechanism.)
5. **Test**: invoke the agent (via `agentcore invoke --bearer-token <cognito-id-token>` or the frontend), then check Datadog APM + LLM Observability for the resulting trace under the `agentcore-iteration-1-llmobs-env-agent` service name.

## Caveats

- This only removes the *LLMObs/ddtrace enable call* from application code. It doesn't change anything about how LangChain/LangGraph or `botocore` get instrumented — that's still `ddtrace`'s automatic patching, same as iteration-1, just triggered by `ddtrace.auto` (via `sitecustomize.py`) instead of an explicit `LLMObs.enable()` call.
- Depends on `direct_code_deploy`'s cwd being the code root at process start (so `PYTHONPATH=.` resolves correctly) — this is an observed behavior of the current AgentCore managed runtime, not a documented contract; worth re-verifying if AWS changes how `direct_code_deploy` processes are launched.
- The `entryPoint` allow-list behavior (only `opentelemetry-instrument` accepted as a wrapper) is also an observed, undocumented API behavior — noted here in case it changes and makes the `ddtrace-run`-via-`entryPoint` approach viable later.

## Cleanup

```bash
aws cloudformation delete-stack --stack-name agentcore-api   # if you deployed API Gateway for this
cd agent && agentcore destroy
# Don't delete the shared Cognito stack if other iterations still use it
```
