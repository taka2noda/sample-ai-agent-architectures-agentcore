import os
import json
import urllib.request
from datetime import datetime

from bedrock_agentcore.runtime import BedrockAgentCoreApp
from langchain_aws import ChatBedrockConverse
from langgraph.prebuilt import create_react_agent

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.extension.aws.trace import AwsXRayIdGenerator
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.exporter.otlp.proto.http import Compression
import requests
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.session import Session as BotocoreSession
from requests.auth import AuthBase

app = BedrockAgentCoreApp()

AWS_REGION = os.environ.get("AWS_REGION", "us-west-2")
MODEL_ID = "global.anthropic.claude-haiku-4-5-20251001-v1:0"


class SigV4Session(AuthBase):
    """Signs outgoing OTLP/HTTP requests with SigV4 so they're accepted by the
    AWS X-Ray direct OTLP endpoint (which has no local collector to sign for
    us - see PROGRESS.md for how we confirmed there's no local receiver)."""

    def __init__(self, service: str, region: str):
        self._service = service
        self._region = region
        self._credentials = BotocoreSession().get_credentials()

    def __call__(self, prepared_request):
        aws_request = AWSRequest(
            method=prepared_request.method,
            url=prepared_request.url,
            headers={"Content-Type": prepared_request.headers.get("Content-Type", "application/x-protobuf")},
            data=prepared_request.body or b"",
        )
        SigV4Auth(self._credentials.get_frozen_credentials(), self._service, self._region).add_auth(aws_request)
        prepared_request.headers.update(dict(aws_request.headers))
        return prepared_request


def _setup_dual_ship_tracer():
    """Dual-ship experiment: one TracerProvider, two BatchSpanProcessors -
    one exporting via OTLP+SigV4 straight to the AWS X-Ray OTLP endpoint
    (no collector), one exporting via OTLP straight to Datadog's direct OTLP
    trace intake (also no collector/Agent). AgentCore Runtime doesn't run a
    local OTLP receiver and never configures an in-process SDK
    TracerProvider itself (confirmed empirically), so this is a from-scratch
    setup, not something layered onto an existing provider.
    """
    resource = Resource.create({
        "service.name": os.environ.get("OTEL_SERVICE_NAME", "agentcore-iteration-1-otel-agent"),
        "deployment.environment": os.environ.get("DD_ENV", "sandbox"),
    })
    provider = TracerProvider(resource=resource, id_generator=AwsXRayIdGenerator())

    xray_session = requests.Session()
    xray_session.auth = SigV4Session(service="xray", region=AWS_REGION)
    xray_exporter = OTLPSpanExporter(
        endpoint=f"https://xray.{AWS_REGION}.amazonaws.com/v1/traces",
        compression=Compression.NoCompression,
        session=xray_session,
    )
    provider.add_span_processor(BatchSpanProcessor(xray_exporter))

    dd_api_key = os.environ.get("DD_API_KEY")
    if dd_api_key:
        datadog_exporter = OTLPSpanExporter(
            endpoint="https://otlp.datadoghq.com/v1/traces",
            headers={"dd-api-key": dd_api_key},
        )
        provider.add_span_processor(BatchSpanProcessor(datadog_exporter))

    trace.set_tracer_provider(provider)
    return trace.get_tracer("agentcore-iteration-1-otel-agent")


tracer = _setup_dual_ship_tracer()


def get_current_time() -> str:
    """Get the current date and time."""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def get_weather(latitude: float, longitude: float) -> str:
    """Get the current weather forecast for a location using lat/long coordinates.

    Examples:
    - New York: latitude=40.7128, longitude=-74.0060
    - Los Angeles: latitude=34.0522, longitude=-118.2437
    - Seattle: latitude=47.6062, longitude=-122.3321
    """
    try:
        points_url = f"https://api.weather.gov/points/{latitude},{longitude}"
        req = urllib.request.Request(points_url, headers={"User-Agent": "AgentCoreSample"})
        with urllib.request.urlopen(req, timeout=10) as response:
            points_data = json.loads(response.read().decode())

        forecast_url = points_data["properties"]["forecast"]
        if not forecast_url.startswith("https://"):
            return "Unable to get weather: invalid URL scheme"
        req = urllib.request.Request(forecast_url, headers={"User-Agent": "AgentCoreSample"})
        with urllib.request.urlopen(req, timeout=10) as response:
            forecast_data = json.loads(response.read().decode())

        period = forecast_data["properties"]["periods"][0]
        return f"{period['name']}: {period['temperature']}°{period['temperatureUnit']}, {period['shortForecast']}"
    except Exception as e:
        return f"Unable to get weather: {e}"


TOOLS = [get_current_time, get_weather]

_agent = None


def get_agent():
    global _agent
    if _agent is None:
        llm = ChatBedrockConverse(model=MODEL_ID, region_name=AWS_REGION)
        _agent = create_react_agent(llm, TOOLS)
    return _agent


@app.entrypoint
def invoke(payload, context):
    prompt = payload.get("prompt", "Hello!") if payload else "Hello!"

    with tracer.start_as_current_span("agentcore.invoke") as span:
        span.set_attribute("agentcore.prompt_length", len(prompt))
        result = get_agent().invoke({"messages": [("human", prompt)]})

        for msg in reversed(result.get("messages", [])):
            if hasattr(msg, 'content') and msg.type == "ai":
                content = msg.content
                if isinstance(content, str):
                    span.set_attribute("agentcore.response_length", len(content))
                    return {"response": content}
                elif isinstance(content, list):
                    text = "".join(b.get("text", b) if isinstance(b, dict) else b for b in content)
                    span.set_attribute("agentcore.response_length", len(text))
                    return {"response": text}
        return {"response": ""}


if __name__ == "__main__":
    app.run()
