import asyncio

from reactor_agent.mcp_server import create_mcp_server
from reactor_agent.service import ReactorAgentService


def test_mcp_exposes_only_domain_tools(tmp_path):
    service = ReactorAgentService(data_dir=tmp_path, database=None)
    server = create_mcp_server(service)
    tools = asyncio.run(server.list_tools())
    names = {tool.name for tool in tools}
    assert names == {"list_backend_capabilities", "validate_model", "create_case", "queue_model_run", "get_task_audit", "interpret_request", "get_model_spec", "get_simulation_results"}
    assert all("COM" not in tool.name for tool in tools)



def test_mcp_nested_spec_schema_and_safe_case_names(tmp_path):
    from reactor_agent.mcp_server import PathLike
    server = create_mcp_server(ReactorAgentService(data_dir=tmp_path))
    tools = asyncio.run(server.list_tools())
    validate = next(tool for tool in tools if tool.name == "validate_model")
    assert "spec" in validate.inputSchema["properties"]
    assert "passed" in validate.outputSchema["properties"]
    for name in ["../other.hsc", "C:/other.hsc", "case:stream.hsc"]:
        assert PathLike(name).suffix == ""


def test_mcp_refuses_non_loopback_host(tmp_path, monkeypatch):
    import pytest
    monkeypatch.setenv("MCP_HOST", "0.0.0.0")
    with pytest.raises(ValueError, match="loopback"):
        create_mcp_server(ReactorAgentService(data_dir=tmp_path))
