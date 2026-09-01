import os

from ddtrace.llmobs import LLMObs

LLMObs.enable(
    ml_app=os.environ.get("DD_LLMOBS_ML_APP_NAME", "agentcore-iteration-1-mcp-datadog-agent"),
    api_key=os.environ.get("DD_API_KEY"),
    site=os.environ.get("DD_SITE", "datadoghq.com"),
    agentless_enabled=True,
)

import asyncio
import json
import urllib.request
from datetime import datetime

from bedrock_agentcore.runtime import BedrockAgentCoreApp
from langchain_aws import ChatBedrockConverse
from langchain_mcp_adapters.client import MultiServerMCPClient
from langgraph.prebuilt import create_react_agent

app = BedrockAgentCoreApp()

AWS_REGION = os.environ.get("AWS_REGION", "us-west-2")
MODEL_ID = "global.anthropic.claude-haiku-4-5-20251001-v1:0"

DD_SITE = os.environ.get("DD_SITE", "datadoghq.com")
DD_MCP_TOKEN = os.environ.get("DD_MCP_TOKEN")
DD_MCP_TOOLSETS = os.environ.get("DD_MCP_TOOLSETS", "apm,llmobs")
DD_MCP_URL = f"https://mcp.{DD_SITE}/api/unstable/mcp-server/mcp?toolsets={DD_MCP_TOOLSETS}"


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


LOCAL_TOOLS = [get_current_time, get_weather]

_agent = None


async def _load_datadog_mcp_tools():
    """Connect to the Datadog MCP Server over streamable HTTP, authenticated with a
    Service Account Token (SAT) passed as a bearer token. Returns [] (agent still
    works with LOCAL_TOOLS only) if no token is configured, so the agent doesn't
    hard-fail just because this particular capability isn't set up yet.
    """
    if not DD_MCP_TOKEN:
        return []
    client = MultiServerMCPClient({
        "datadog": {
            "transport": "streamable_http",
            "url": DD_MCP_URL,
            "headers": {"Authorization": f"Bearer {DD_MCP_TOKEN}"},
        }
    })
    tools = await client.get_tools()

    # A handful of Datadog MCP tools (zero-argument/internal ones, e.g.
    # get_user_config) return a JSON schema with no "properties" key.
    # langchain_core.tools.base.BaseTool.args assumes "properties" always
    # exists and raises KeyError otherwise -- and so does ddtrace's LangGraph
    # integration, which reads the same property to build its LLM
    # Observability tool manifest at create_react_agent() time, crashing
    # every invocation before it even runs. Drop those tools rather than
    # crash agent initialization.
    usable_tools = []
    for tool in tools:
        try:
            tool.args
        except KeyError:
            continue
        usable_tools.append(tool)
    return usable_tools


def get_agent():
    global _agent
    if _agent is None:
        llm = ChatBedrockConverse(model=MODEL_ID, region_name=AWS_REGION)
        datadog_tools = asyncio.run(_load_datadog_mcp_tools())
        _agent = create_react_agent(llm, LOCAL_TOOLS + datadog_tools)
    return _agent


@app.entrypoint
def invoke(payload, context):
    prompt = payload.get("prompt", "Hello!") if payload else "Hello!"
    # langchain-mcp-adapters tools only implement the async _arun, not sync
    # _run, so calling create_react_agent's graph via the sync .invoke()
    # raises "StructuredTool does not support sync invocation" as soon as
    # LangGraph's ToolNode tries to call one. Using .ainvoke() (run via
    # asyncio.run, since this handler itself runs synchronously in a worker
    # thread with no event loop) lets LangGraph await the tool's async path.
    result = asyncio.run(get_agent().ainvoke({"messages": [("human", prompt)]}))

    for msg in reversed(result.get("messages", [])):
        if hasattr(msg, 'content') and msg.type == "ai":
            content = msg.content
            if isinstance(content, str):
                return {"response": content}
            elif isinstance(content, list):
                text = "".join(b.get("text", b) if isinstance(b, dict) else b for b in content)
                return {"response": text}
    return {"response": ""}


if __name__ == "__main__":
    app.run()
