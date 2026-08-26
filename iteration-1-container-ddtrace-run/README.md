# Iteration 1 (container variant): Dockerfile CMD edit to prepend ddtrace-run

This is a copy of [iteration-1](../iteration-1/) used to test the idea that got ruled out for [`iteration-1-llmobs-env`](../iteration-1-llmobs-env/): **editing the deployment's `Dockerfile` `CMD` to prepend `ddtrace-run`, instead of a `sitecustomize.py`/`PYTHONPATH` trick or app code changes.**

That idea doesn't apply to `deployment_type: direct_code_deploy` (no Dockerfile exists there at all). This directory uses `deployment_type: container` instead, specifically so a Dockerfile exists to edit. It uses its own agent name (`agent_1_container_ddtrace_run`) and doesn't touch any other iteration's deployed agent.

## TL;DR answer

**Yes, this works too**, and it's arguably simpler than the `sitecustomize.py` approach when `container` deployment is already in use: `agent/agent.py` has zero `ddtrace`/`LLMObs` code, and the only non-standard thing is a one-line edit to the `Dockerfile`'s last line.

## How

1. `agentcore configure --deployment-type container ...` generates a default `Dockerfile` into the toolkit's cache directory (`.bedrock_agentcore/<agent_name>/Dockerfile`, gitignored) with:
   ```dockerfile
   CMD ["opentelemetry-instrument", "python", "-m", "agent"]
   ```
   (the `opentelemetry-instrument` wrapper is AWS's own GenAI Observability instrumentation, on by default; unrelated to Datadog.)
2. Per `bedrock_agentcore_starter_toolkit/utils/runtime/container.py::generate_dockerfile`, if a `Dockerfile` **already exists in the agent's project root** (`agent/Dockerfile`, tracked in this repo), `agentcore configure` copies that file verbatim into the cache directory instead of regenerating from the template — logged as `📄 Using existing Dockerfile from: .../agent/Dockerfile`. This *only* happens during `agentcore configure`, not `agentcore deploy` — so the edit has to exist before configuring (or you re-run `configure` after adding it, which is safe to do against an already-deployed agent; it only rewrites local config/cache files).
3. `agent/Dockerfile` in this directory is a trimmed copy of the generated one — same base image/deps/user setup, but with AWS's `aws-opentelemetry-distro` install and `opentelemetry-instrument` wrapping removed (to keep this a clean, isolated ddtrace-only test) and the last line changed to:
   ```dockerfile
   CMD ["ddtrace-run", "python", "-m", "agent"]
   ```
4. `agentcore deploy` (no flags — CodeBuild builds the ARM64 image in the cloud, no local Docker/Colima needed) packages whatever Dockerfile is sitting in the cache directory into the CodeBuild source zip and builds it as-is.

## What we verified

Deployed `agent_1_container_ddtrace_run` (container deployment, us-west-2, same shared Cognito stack as iteration-1) and invoked it with a real Cognito JWT:

- CloudWatch logs for the invocation show ddtrace's own startup log lines (`OpenTelemetry configuration OTEL_PYTHON_DISTRO/CONFIGURATOR/EXCLUDED_URLS is not supported by Datadog`) — this message only exists in `ddtrace`'s codebase and only fires when `ddtrace-run`/`ddtrace.auto` boots, confirming the `CMD` edit took effect and ddtrace loaded.
- Datadog APM shows the trace for the invocation: a `starlette.request` root span (`POST /invocations`), tagged `service:agentcore-iteration-1-container-ddtrace-run-agent`, `env:sandbox`, with `llmobs_trace_id`/`llmobs_parent_id` present (LLM Observability correlation working), same as the `sitecustomize.py`-based variant.

## Comparison with `iteration-1-llmobs-env` (the `sitecustomize.py` approach)

| | `iteration-1-llmobs-env` (`direct_code_deploy`) | `iteration-1-container-ddtrace-run` (`container`) |
|---|---|---|
| App code changes | none | none |
| Non-code changes | `sitecustomize.py` (1 new file) + `PYTHONPATH=.` env var | `Dockerfile` `CMD` line edit (1 line) |
| Requires | nothing extra | `deployment_type: container` (CodeBuild-built image; no local Docker needed since `agentcore deploy` builds it in the cloud) |
| Applies to iteration-0/1/2/3 as they are today? | Yes — they're all `direct_code_deploy` | No — would require switching those agents to `deployment_type: container` first |

Since iteration-0/1/2/3 in this repo are all `direct_code_deploy` today, the `sitecustomize.py` approach in `iteration-1-llmobs-env` is the more directly applicable one for the rest of this repo. This directory exists to confirm the Dockerfile idea *specifically because* it couldn't be tested against those iterations as configured, not because it's the recommended path here.

## Setup

Same shape as iteration-1 (shared Cognito stack, OAuth pass-through):

1. **Prerequisites**: same shared `agentcore-cognito` stack and test user as iteration-1.
2. **Configure**: `cd agent && agentcore configure --deployment-type container ...` (see iteration-1's README for the equivalent interactive prompts) with a distinct agent name (`agent_1_container_ddtrace_run`). Make sure `agent/Dockerfile` (this directory's already-edited copy) exists in the project root *before* running configure, so it gets picked up instead of the template default.
3. **Deploy**: `agentcore deploy --env "DD_API_KEY=${DD_API_KEY}" --env "DD_SITE=datadoghq.com" --env "DD_ENV=sandbox" --env "DD_SERVICE=agentcore-iteration-1-container-ddtrace-run-agent" --env "DD_LLMOBS_ENABLED=1" --env "DD_LLMOBS_ML_APP=agentcore-iteration-1-container-ddtrace-run-agent" --env "DD_LLMOBS_AGENTLESS_ENABLED=1" --env "DD_TRACE_LANGCHAIN_ENABLED=false" --env 'DD_TRACE_SAMPLING_RULES=[{"resource": "GET /ping", "sample_rate": 0}]'` — no `--local-build`/Docker required, CodeBuild does the ARM64 build.
4. **Test**: `agentcore invoke '{"prompt": "..."}' --bearer-token <cognito-id-token>`, then check Datadog APM for the trace.

## Caveats

- The "user Dockerfile in project root gets reused verbatim" behavior is an internal detail of the current `bedrock-agentcore-starter-toolkit` (`container.py::generate_dockerfile`), not a documented public contract — worth re-checking if the toolkit changes.
- This custom `Dockerfile` intentionally drops AWS's own `opentelemetry-instrument` wrapping (`aws-opentelemetry-distro`) to avoid mixing two tracing pipelines in one test. If you want both AWS's GenAI Observability *and* Datadog active on the same container, you'd need `CMD ["ddtrace-run", "opentelemetry-instrument", "python", "-m", "agent"]` — not tested here.

## Cleanup

```bash
cd agent && agentcore destroy
# Don't delete the shared Cognito stack if other iterations still use it
```
