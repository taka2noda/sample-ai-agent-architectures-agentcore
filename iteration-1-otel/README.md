# Iteration 1 (OTel variant): Dual-shipping telemetry to AWS CloudWatch/X-Ray *and* Datadog via OpenTelemetry

This is a copy of [iteration-1](../iteration-1/) (Browser → Amazon API Gateway → Amazon Bedrock AgentCore Runtime, OAuth pass-through) used to answer a specific question: **can a Bedrock AgentCore agent send the same trace data to both AWS CloudWatch/X-Ray and Datadog using OpenTelemetry, instead of Datadog's native `ddtrace` library?**

It does not touch `iteration-1`'s own deployed agent (`agent_1`) — this uses a separate agent name (`agent_1_otel`) so both can coexist.

## TL;DR answer

**Yes, but not by "adding Datadog as a second export target" to something AgentCore already runs.** There is no existing in-process OpenTelemetry pipeline to extend (see below). The working approach is for the application to own its own OpenTelemetry SDK setup and fan out to **two independent, collector-less direct-OTLP endpoints** — one for AWS X-Ray, one for Datadog — from a single `TracerProvider`.

## What we found investigating AgentCore's own OTel setup

`agentcore deploy` always auto-configures a set of `OTEL_*` environment variables on the runtime (`OTEL_PYTHON_DISTRO=aws_distro`, `OTEL_PYTHON_CONFIGURATOR=aws_configurator`, `OTEL_EXPORTER_OTLP_TRACES_HEADERS` with `x-aws-log-group`/`x-aws-log-stream`, `OTEL_RESOURCE_ATTRIBUTES`, `OTEL_PYTHON_EXCLUDED_URLS=/ping`), and every `agentcore deploy` logs "Traces delivery enabled" — data does show up in the GenAI Observability Dashboard / X-Ray. It's reasonable to assume from this that OpenTelemetry auto-instrumentation is running inside your Python process. **It isn't.**

We probed the running agent process at three points — before any other import, right after `BedrockAgentCoreApp()` is constructed, and inside the request handler at actual invoke time — by dumping `opentelemetry.trace.get_tracer_provider()`. At every point it was the SDK's default `ProxyTracerProvider` (a no-op placeholder), never a real configured `TracerProvider`. We also tried POSTing to `http://localhost:4318/v1/traces` (the OTel SDK's default local OTLP/HTTP receiver address) from inside the running agent — `ConnectionRefused`.

**Conclusion:** whatever produces AgentCore's own CloudWatch/X-Ray telemetry happens entirely outside the customer's Python process, at the AWS platform/infrastructure layer. There is no shared `TracerProvider` or local collector for application code to attach a second (Datadog-facing) exporter to.

## How dual-ship actually works here

Both AWS and Datadog offer **collector-less, direct OTLP trace ingestion** — no OpenTelemetry Collector or Datadog Agent needed:

| Target | Endpoint | Auth |
|---|---|---|
| AWS X-Ray | `https://xray.<region>.amazonaws.com/v1/traces` | SigV4-signed request (needs `xray:PutTraceSegments` + `xray:PutTelemetryRecords` IAM permissions — already present on the agent's default execution role) |
| Datadog | `https://otlp.datadoghq.com/v1/traces` | `dd-api-key` header, no signing |

`agent/agent.py` sets up **one `TracerProvider`** with **two `BatchSpanProcessor`s**, each wrapping its own `OTLPSpanExporter` pointed at one of the endpoints above, then wraps the agent invocation in a single span (`agentcore.invoke`). That one span gets independently exported to both backends.

Two gotchas that will silently break this if missed:
- **`AwsXRayIdGenerator()` is required** as the `TracerProvider`'s `id_generator`. X-Ray requires the first 4 bytes of a trace ID to encode a Unix timestamp; without this generator, traces are accepted (no error) but never appear in X-Ray/CloudWatch.
- **The OTLP exporter's `session=` kwarg expects a `requests.Session` object**, not an auth callable. Passing a custom SigV4-signing object directly as `session=` throws `AttributeError: 'SigV4Session' object has no attribute 'headers'`. Instead, create a real `requests.Session()` and set `.auth` on it to the SigV4 callable.
- Your account's X-Ray trace segment destination must be `CloudWatchLogs` (`aws xray get-trace-segment-destination` — this is typically already `ACTIVE` if you've run `agentcore deploy` before, since AgentCore's own observability setup configures it).

## Verifying it worked

After invoking the agent, the same trace_id should be queryable in both places:

```bash
# Datadog (via MCP or the UI) - search for service:<your OTEL_SERVICE_NAME>
# Look for tag ingestion_reason:otel to confirm it came through direct OTLP, not ddtrace

# AWS X-Ray - the trace ID format differs (dashes inserted), e.g.
# Datadog trace_id 6a8686f717b8f5fb0d237fc174c1830d ==
# X-Ray trace ID    1-6a8686f7-17b8f5fb0d237fc174c1830d
aws xray get-trace-summaries --region <region> \
  --start-time $(date -u -v-15M +%s) --end-time $(date -u +%s)
aws xray batch-get-traces --trace-ids <the-matching-id> --region <region>
```

We confirmed this end-to-end: the `agentcore.invoke` span (with custom attributes `agentcore.prompt_length`/`agentcore.response_length` set in code) appeared in both Datadog and X-Ray under the exact same trace ID.

## Caveats to flag before using this pattern for real

- This is bespoke application code, not a documented/supported AgentCore or Datadog feature — nothing prevents it from working, but there's no vendor guarantee it keeps working if AWS changes AgentCore's internal telemetry plumbing.
- It's unrelated to and doesn't interact with the `ddtrace`-based Datadog instrumentation used in iterations 0-3 — the two are separate, uncorrelated tracing pipelines if both were ever present in the same agent process (not attempted here; this experiment intentionally started from a clean copy to avoid interference).
- Datadog's direct OTLP intake is intended for exactly this kind of serverless/managed-platform scenario where running a Collector or Agent isn't possible; for production workloads in general, Datadog recommends routing through a Collector/Agent for metadata enrichment and centralized sampling where that's an option (it isn't, here, inside AgentCore Runtime's sandbox).

## Setup

Follows the same deployment shape as [iteration-1](../iteration-1/README.md) (shared Cognito stack, OAuth pass-through, API Gateway + WAF), with these differences:

1. **Prerequisites**: same as iteration-1 (shared `agentcore-cognito` stack, test user) — see that iteration's README for those steps.
2. **Agent dependencies** (`agent/requirements.txt`): `opentelemetry-api`, `opentelemetry-sdk`, `opentelemetry-exporter-otlp-proto-http`, `opentelemetry-sdk-extension-aws`, `botocore`, `requests`, in addition to the usual `bedrock-agentcore`/`langchain`/`langgraph` set.
3. **Configure and deploy the agent** — same `agentcore configure`/`agentcore deploy` flow as iteration-1, but use a distinct agent name (e.g. `agent_1_otel`) so you don't collide with an already-deployed `agent_1`. Pass the Datadog API key and service name as env vars:
   ```bash
   agentcore deploy \
     --env "DD_API_KEY=${DD_API_KEY}" \
     --env "DD_ENV=sandbox" \
     --env "OTEL_SERVICE_NAME=agentcore-iteration-1-otel-agent"
   ```
4. **API Gateway + frontend**: identical to iteration-1 — deploy `api-gateway.yaml` with the new agent's Runtime ID, update `frontend/index.html`'s `CONFIG`.
5. **Test**: invoke the agent (via `agentcore invoke` or the frontend) and check both Datadog and X-Ray for the resulting trace, as described above.

## Cleanup

```bash
aws cloudformation delete-stack --stack-name agentcore-api   # if you deployed API Gateway for this
cd agent && agentcore destroy
# Don't delete the shared Cognito stack if other iterations still use it
```
