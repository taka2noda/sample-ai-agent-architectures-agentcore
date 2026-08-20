## Overview

This repository contains different architectural patterns for deploying AI agents on AWS using Amazon Bedrock AgentCore Runtime. Each iteration builds on the previous, showing a progression from simple to production-ready.

All agents included are simple prototypes using LangGraph but could be extended for different use cases. The focus of this is the surrounding architectural components, not the agent functionality itself.

> **This is a fork** of [aws-samples/sample-ai-agent-architectures-agentcore](https://github.com/aws-samples/sample-ai-agent-architectures-agentcore), extended to add **Datadog observability** (RUM, APM, and LLM/Agent Observability) on top of each AWS architecture pattern. See [Datadog Observability](#datadog-observability-this-fork) below for what was added and known issues.

## Datadog Observability (this fork)

Each iteration is instrumented for Datadog in addition to its AWS architecture, following a consistent per-iteration pattern: get the app working first, then add Datadog on top of it (frontend RUM, agent-side APM/LLM Observability, and Lambda APM where a Lambda is present).

- **RUM + Logs** — added to every iteration's `frontend/index.html` via the Datadog Browser SDK. Requires a Datadog RUM Application (`applicationId` + `clientToken`) per iteration/environment.
- **APM + LLM/Agent Observability (the AgentCore agent)** — `ddtrace` + `ddtrace.llmobs.LLMObs.enable(agentless_enabled=True)` added as the first import in each `agent/agent.py`, configured via environment variables passed with `agentcore deploy --env KEY=VALUE` (not stored in any file): `DD_API_KEY`, `DD_SITE`, `DD_LLMOBS_ML_APP_NAME`, `DD_ENV`, `DD_SERVICE`, `DD_TRACE_LANGCHAIN_ENABLED=false`.
- **Lambda APM (iteration-2 and 3)** — instrumented via the [Datadog Serverless Macro](https://docs.datadoghq.com/serverless/libraries_integrations/macro/) added to each `template.yaml`'s `Transform` section, rather than manually wiring the Datadog Lambda layer/extension.
- **Trace correlation across the Lambda → AgentCore boundary** — `invoke_agent_runtime` is a SigV4-signed AWS SDK call, not HTTP, so Datadog can't propagate trace context automatically. `iteration-2/lambda/app.py` and `iteration-2/agent/agent.py` manually inject/extract the trace context through the JSON payload, and the agent opens a real child span (`tracer.start_span(child_of=..., activate=True)`) before invoking the LangGraph agent — a bare `tracer.context_provider.activate()` is *not* sufficient, because LangGraph's Pregel runtime executes nodes via `concurrent.futures.ThreadPoolExecutor`, and ddtrace's cross-thread propagation only carries an active Span, not a span-less remote Context.

### Datadog Setup Steps

Do this **after** the AWS side of an iteration is deployed and working (see [Getting Started](#getting-started) and that iteration's own README). Repeat per iteration — `<N>` below is the iteration number (`0`, `1`, `2`, ...).

**0. Prerequisites**

```bash
export DD_API_KEY=<your Datadog API key>
export DD_APP_KEY=<your Datadog Application key>
export DD_SITE=datadoghq.com   # or your org's site, e.g. us5.datadoghq.com
```

**1. RUM + Logs (frontend)**

Create a RUM Browser Application (there's no dedicated CLI for this — use the API directly):

```bash
curl -s -X POST "https://api.${DD_SITE}/api/v2/rum/applications" \
  -H "DD-API-KEY: ${DD_API_KEY}" \
  -H "DD-APPLICATION-KEY: ${DD_APP_KEY}" \
  -H "Content-Type: application/json" \
  -d '{
    "data": {
      "type": "rum_application_create",
      "attributes": { "name": "agentcore-sample-iteration-<N>", "type": "browser" }
    }
  }'
```

Take the `applicationId` and `clientToken` from the response and add this to the very top of `iteration-<N>/frontend/index.html`'s `<head>` (before any other `<script>` tags):

```html
<script>
  (function(h,o,u,n,d) {
    h=h[d]=h[d]||{q:[],onReady:function(c){h.q.push(c)}}
    d=o.createElement(u);d.async=1;d.src=n
    n=o.getElementsByTagName(u)[0];n.parentNode.insertBefore(d,n)
  })(window,document,'script','https://www.datadoghq-browser-agent.com/us1/v6/datadog-rum.js','DD_RUM')

  window.DD_RUM.onReady(function() {
    window.DD_RUM && window.DD_RUM.init({
      applicationId: '<RUM_APPLICATION_ID>',
      clientToken: '<RUM_CLIENT_TOKEN>',
      site: '<DD_SITE>',
      service: 'agentcore-iteration-<N>-frontend',
      env: 'sandbox',
      version: '1.0.0',
      sessionSampleRate: 100,
      sessionReplaySampleRate: 20,
      trackUserInteractions: true,
      trackResources: true,
      trackLongTasks: true,
    });
  });
</script>
<script>
  (function(h,o,u,n,d) {
    h=h[d]=h[d]||{q:[],onReady:function(c){h.q.push(c)}}
    d=o.createElement(u);d.async=1;d.src=n
    n=o.getElementsByTagName(u)[0];n.parentNode.insertBefore(d,n)
  })(window,document,'script','https://www.datadoghq-browser-agent.com/us1/v6/datadog-logs.js','DD_LOGS')
  window.DD_LOGS.onReady(function() {
    window.DD_LOGS.init({
      clientToken: '<RUM_CLIENT_TOKEN>',
      site: '<DD_SITE>',
      forwardErrorsToLogs: true,
    })
  })
</script>
```

**2. APM + LLM/Agent Observability (the AgentCore agent)**

Add `ddtrace` to `iteration-<N>/agent/requirements.txt`, then in `iteration-<N>/agent/agent.py`, make `LLMObs.enable(...)` the very first thing that runs — before any other imports:

```python
import os

from ddtrace.llmobs import LLMObs

LLMObs.enable(
    ml_app=os.environ.get("DD_LLMOBS_ML_APP_NAME", "agentcore-iteration-<N>-agent"),
    api_key=os.environ.get("DD_API_KEY"),
    site=os.environ.get("DD_SITE", "datadoghq.com"),
    agentless_enabled=True,
)

# ... the rest of the original imports (json, bedrock_agentcore, langgraph, etc.) follow after this
```

Reinstall deps in the agent's venv, then redeploy the agent with the Datadog env vars (`agentcore deploy` persists these directly on the runtime resource — they are not written to any file):

```bash
cd iteration-<N>/agent
source .venv/bin/activate && uv pip install -r requirements.txt

AGENTCORE_SUPPRESS_RECOMMENDATION=1 agentcore deploy \
  --env "DD_API_KEY=${DD_API_KEY}" \
  --env "DD_SITE=${DD_SITE}" \
  --env "DD_LLMOBS_ML_APP_NAME=agentcore-iteration-<N>-agent" \
  --env "DD_ENV=sandbox" \
  --env "DD_SERVICE=agentcore-iteration-<N>-agent" \
  --env "DD_TRACE_LANGCHAIN_ENABLED=false"
```

`DD_TRACE_LANGCHAIN_ENABLED=false` is **required**, not optional — see [Known issues / gotchas](#known-issues--gotchas) below.

**3. Lambda APM (iteration-2 and iteration-3 only)**

The [Datadog Serverless Macro](https://docs.datadoghq.com/serverless/libraries_integrations/macro/) needs to be installed once per AWS account/region:

```bash
aws cloudformation create-stack \
  --stack-name datadog-serverless-macro \
  --template-url https://datadog-cloudformation-template.s3.amazonaws.com/aws/serverless-macro/latest.yml \
  --capabilities CAPABILITY_AUTO_EXPAND CAPABILITY_IAM
```

Then, in `iteration-<N>/template.yaml`, extend `Transform` from a single string into a list, and add a `DatadogApiKey` parameter (`NoEcho: true` — the value is passed in at deploy time, never hardcoded):

```yaml
Transform:
  - AWS::Serverless-2016-10-31
  - Name: DatadogServerless
    Parameters:
      stackName: !Ref "AWS::StackName"
      apiKey: !Ref DatadogApiKey
      pythonLayerVersion: "<LATEST_PYTHON_LAYER_VERSION>"      # see: curl -s https://api.github.com/repos/DataDog/datadog-lambda-python/releases/latest
      extensionLayerVersion: "<LATEST_EXTENSION_LAYER_VERSION>" # see: curl -s https://api.github.com/repos/DataDog/datadog-lambda-extension/releases/latest
      service: agentcore-iteration-<N>-chat
      env: sandbox
      site: datadoghq.com

Parameters:
  # ... existing parameters ...
  DatadogApiKey:
    Type: String
    NoEcho: true
```

The macro requires each instrumented function's `FunctionName` to be a **literal string**, not a `!Sub`/intrinsic expression (it needs the concrete name to wire up log subscriptions).

> **Gotcha with multiple functions in one template (iteration-3)**: setting a per-function service name via `Metadata: {DatadogServerless: {service: ...}}` on each `AWS::Serverless::Function` resource did **not** actually set `DD_SERVICE` (verified with `aws lambda get-function-configuration` — it was simply absent). The reliable fix is to set `DD_SERVICE` as a plain `Environment.Variables` entry directly on each function instead:
> ```yaml
>       Environment:
>         Variables:
>           DD_SERVICE: agentcore-iteration-<N>-chat   # set directly per function; don't rely on Metadata.service
> ```

Then build and deploy with the key passed as a parameter override:

```bash
cd iteration-<N>
sam build
sam deploy --parameter-overrides \
  "AgentCoreRuntimeArn=<your agent ARN>" \
  "CognitoUserPoolArn=<your Cognito user pool ARN>" \
  "DatadogApiKey=${DD_API_KEY}"
```

**4. Trace correlation across the Lambda → AgentCore call (iteration-2 and iteration-3)**

`invoke_agent_runtime` is an IAM SigV4-signed AWS SDK call, not HTTP, so Datadog cannot propagate trace context automatically the way it does across real HTTP hops. To connect the Lambda's trace with the agent's LLM-call spans into one trace, the trace context is manually smuggled through the JSON payload:

In the Lambda (`lambda/app.py`), inject the current trace context into the request payload:

```python
from ddtrace import tracer
from ddtrace.propagation.http import HTTPPropagator

dd_trace_headers = {}
HTTPPropagator.inject(tracer.current_trace_context(), dd_trace_headers)

response = client.invoke_agent_runtime(
    agentRuntimeArn=AGENTCORE_RUNTIME_ARN,
    qualifier="DEFAULT",
    payload=json.dumps({"prompt": message, "_datadog_trace_headers": dd_trace_headers})
)
```

In the agent (`agent/agent.py`), extract it and open a **real child span** before invoking the LangGraph agent — merely `tracer.context_provider.activate(...)` on the extracted context is *not* enough (see gotcha below):

```python
import contextlib

from ddtrace import tracer
from ddtrace.propagation.http import HTTPPropagator

dd_trace_headers = payload.get("_datadog_trace_headers") if payload else None
dd_context = HTTPPropagator.extract(dd_trace_headers) if dd_trace_headers else None

# Span implements the context-manager protocol (auto-finish on exit), so use
# nullcontext() when there's no incoming trace context instead of duplicating
# the invoke() call across an if/else.
span_ctx = (
    tracer.start_span("agentcore.invoke", child_of=dd_context, service=os.environ.get("DD_SERVICE"), activate=True)
    if dd_context and dd_context.trace_id
    else contextlib.nullcontext()
)
with span_ctx:
    result = get_agent().invoke({"messages": [("human", prompt)]})
```

### Known issues / gotchas

- **LangGraph + ddtrace crash**: enabling `LLMObs.enable(...)` on an agent that uses LangGraph tools can crash real requests (not just tracing) the moment a tool executes, due to a ddtrace bug ([dd-trace-py#18561](https://github.com/DataDog/dd-trace-py/issues/18561)) where a non-JSON-serializable object leaks into span metadata. Workaround: set `DD_TRACE_LANGCHAIN_ENABLED=false` on the agent (do **not** also disable `DD_TRACE_LANGGRAPH_ENABLED` — that avoids the crash too, but destroys the trace's workflow structure).
- **AgentCore's own OTel-based observability and Datadog's ddtrace run independently, side by side** — `agentcore deploy` auto-enables an AWS-native OTel pipeline (X-Ray / CloudWatch GenAI Observability Dashboard) on every agent. ddtrace detects this and explicitly does not use it (falls back to its own native instrumentation instead), so the two produce separate, unconnected traces. Verified both are actually receiving live data (not just "configured") via `aws xray get-trace-summaries`/`batch-get-traces`.
- **AgentCore CLI is deprecated in favor of `@aws/agentcore`**: this repo (and this fork's instrumentation) uses `bedrock-agentcore-starter-toolkit` (`pip install bedrock-agentcore`), which prints a deprecation notice on every command. Set `AGENTCORE_SUPPRESS_RECOMMENDATION=1` to silence it.
- **Never run `agentcore destroy` on an agent configured with a shared, externally-managed execution role** (i.e., the role from `cognito.yaml`, not an auto-created `AmazonBedrockAgentCoreSDKRuntime-*` one) without checking dependents first — `destroy` will delete the role even if a CloudFormation stack still manages it as a resource, breaking that stack's next update.

## How to Use This Repository

This repository is designed to be walked through sequentially, starting with the simplest (but least secure) pattern and progressively adding layers of security and functionality.

**Recommended approach:**

1. **Start with Iteration 0** to understand the basics of Amazon Bedrock AgentCore and Amazon Cognito OAuth authentication. This is the quickest way to get an agent running, but exposes the agent directly to the browser.

![Direct client to agent architecture](images/client_to_agent_arch.png)

2. **Move to Iteration 1** to add Amazon API Gateway in front of the agent. This adds rate limiting via AWS WAF, but has a security gap: users get a JWT that works for both the API and the agent directly.

![OAuth integration with AgentCore Runtime](images/oauth_waf_apigateway_agent.png)


3. **Progress to Iteration 2** to fix the security gap by switching to IAM authentication. Now users authenticate to Amazon API Gateway with Amazon Cognito, but the AWS Lambda calls the agent using IAM credentials. Users can no longer bypass your API.

![IAM integration with AgentCore Runtime](images/iteration_2.png)


4. **Finish with Iteration 3** to add conversation persistence using Amazon Bedrock AgentCore Memory and Amazon DynamoDB for a full-featured chat experience.

![IAM integration with AgentCore Runtime with additional functionality for memory](images/iteration_3.png)


You can also jump directly to any iteration if you already understand the tradeoffs, or use a specific iteration as a starting point for your own project.

> **Note**: The Amazon Cognito stack deployed in Iteration 0 is shared across all iterations, so you only need to deploy it once.

## Iterations

### Iteration 0: Direct Browser to Amazon Bedrock AgentCore

**Best for**: Quick prototypes and understanding the basics.

```
Browser → Amazon Bedrock AgentCore Runtime (OAuth via Amazon Cognito)
```

- Simplest possible setup
- Browser calls Amazon Bedrock AgentCore directly
- Amazon Cognito OAuth for authentication
- **Datadog**: RUM+Logs on the frontend, APM + LLM Observability on the agent

[View Iteration 0 →](./iteration-0/)

### Iteration 1: Amazon API Gateway + Amazon Bedrock AgentCore

**Best for**: Adding API management without custom compute.

```
Browser → Amazon API Gateway → Amazon Bedrock AgentCore Runtime (OAuth)
              (Amazon Cognito)
```

- Amazon API Gateway handles rate limiting, request validation
- Amazon Cognito authorizer on Amazon API Gateway
- OAuth JWT pass-through to Amazon Bedrock AgentCore
- **Security note**: User JWT works for both API and agent - not ideal for production
- **Datadog**: RUM+Logs on the frontend, APM + LLM Observability on the agent

[View Iteration 1 →](./iteration-1/)

### Iteration 2: Amazon API Gateway + AWS Lambda + Amazon Bedrock AgentCore (IAM Auth)

**Best for**: Secure production setup with custom compute layer.

```
Browser → Amazon API Gateway → AWS Lambda → Amazon Bedrock AgentCore Runtime (IAM Auth)
              (Amazon Cognito)
```

- AWS Lambda layer for custom logic, logging, input validation
- Agent uses IAM auth - users can't bypass API to call agent directly
- Amazon Cognito validation at Amazon API Gateway level only
- Fixes the security gap in Iteration 1
- **Datadog**: RUM+Logs on the frontend, Lambda APM via the Datadog Serverless Macro, APM + LLM Observability on the agent, with trace correlation across the Lambda → AgentCore call

[View Iteration 2 →](./iteration-2/)

### Iteration 3: Amazon API Gateway + AWS Lambda + Amazon Bedrock AgentCore with Memory

**Best for**: Full-featured chat with conversation persistence.

```
Browser → Amazon API Gateway → AWS Lambda (Chat) → Amazon Bedrock AgentCore Runtime + Memory
                            → AWS Lambda (Conversations) → Amazon Bedrock AgentCore Memory + Amazon DynamoDB
```

- Separate AWS Lambda functions for chat and conversation history
- Amazon Bedrock AgentCore Memory for conversation persistence
- Amazon DynamoDB for conversation metadata (names)
- Auto-generated conversation names
- **Datadog**: RUM+Logs on the frontend, Lambda APM via the Datadog Serverless Macro on both Lambdas, APM + LLM Observability on the agent, with trace correlation across the chat Lambda → AgentCore call (only the `chat` Lambda calls the agent; `conversations` talks to AgentCore Memory/DynamoDB directly and doesn't need it)

[View Iteration 3 →](./iteration-3/)

### Iteration 1 (OTel variant): Dual-shipping to AWS CloudWatch/X-Ray *and* Datadog via OpenTelemetry

**Best for**: Answering "can we send telemetry to both AWS and Datadog via OTel instead of Datadog's native tracer?"

A copy of iteration-1 (doesn't touch its deployed agent) exploring whether AgentCore's own OTel-based observability pipeline can be extended to also ship to Datadog. Short answer: there's no existing in-process pipeline to extend (confirmed empirically — no in-process `TracerProvider`, no local OTLP collector), but genuine dual-ship works by having the app own its own OpenTelemetry SDK setup and fan out to two independent, collector-less direct-OTLP endpoints (AWS X-Ray's and Datadog's). See that folder's README for the full investigation, the working code pattern, and gotchas.

[View Iteration 1 (OTel variant) →](./iteration-1-otel/)

## Prerequisites

- AWS CLI configured with credentials (`aws configure`)
- AWS SAM CLI installed ([installation guide](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html))
- Python 3.11+ (`python3 --version` to check; use `uv python install 3.11` if your system default is older)
- AgentCore CLI (`pip install bedrock-agentcore-starter-toolkit`)
- Bedrock model access enabled in your AWS account (Claude models), in whichever region you deploy to
- A Datadog account, with `DD_API_KEY` / `DD_APP_KEY` available (Application Keys page: `https://<your-org>.datadoghq.com/organization-settings/application-keys`) — needed for the Datadog observability steps in each iteration

> **Tip**: Run `aws sts get-caller-identity` to verify your AWS credentials are working before starting.

## Getting Started

**Start with Iteration 0** - it includes the Cognito stack that's shared across all iterations:

```bash
cd iteration-0

# Deploy Cognito (used by all iterations)
aws cloudformation deploy \
  --template-file cognito.yaml \
  --stack-name agentcore-cognito \
  --capabilities CAPABILITY_NAMED_IAM \
  --region us-east-1
```

> **Note**: Wait for the stack to complete before proceeding. You can check status with:
> ```bash
> aws cloudformation describe-stacks --stack-name agentcore-cognito --query 'Stacks[0].StackStatus'
> ```

```bash
# Create a test user
USER_POOL_ID=$(aws cloudformation describe-stacks \
  --stack-name agentcore-cognito \
  --query 'Stacks[0].Outputs[?OutputKey==`UserPoolId`].OutputValue' \
  --output text)

aws cognito-idp admin-create-user \
  --user-pool-id $USER_POOL_ID \
  --username <YOUR_USERNAME_HERE> \
  --temporary-password <YOUR_PASSWORD_HERE> \
  --message-action SUPPRESS

aws cognito-idp admin-set-user-password \
  --user-pool-id $USER_POOL_ID \
  --username <YOUR_USERNAME_HERE> \
  --password <YOUR_PASSWORD_HERE> \
  --permanent
```

> **Password Requirements**: Must be 8+ characters with uppercase, lowercase, numbers, and special characters

> **Username requirement**: this Cognito User Pool requires `<YOUR_USERNAME_HERE>` to be a valid **email address format** (e.g. `test-user@example.com`) — a plain username like `test-user` will be rejected with `Username should be an email`. It does not need to be a real, deliverable address.

Then follow the README in each iteration folder.

## Repository Structure

```
.
├── iteration-0/        # Direct browser to Amazon Bedrock AgentCore
├── iteration-1/        # Amazon API Gateway + Amazon Bedrock AgentCore (OAuth)
├── iteration-2/        # Amazon API Gateway + AWS Lambda + Amazon Bedrock AgentCore (IAM)
└── iteration-3/        # AWS Lambda + Amazon Bedrock AgentCore with Memory
```

## Security
See CONTRIBUTING for more information.

## License
This library is licensed under the MIT-0 License. See the LICENSE file.
