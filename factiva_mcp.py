"""Optional legacy MCP entry point; the Claude skill calls factiva_api.py directly."""

import sys
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations

sys.path.insert(0, str(Path(__file__).parent / ".claude" / "skills" / "factiva-research" / "scripts"))
import factiva_api as api

mcp = FastMCP("factiva-research", instructions=(
    "Results are retrieved chunks, not verified facts or necessarily complete articles. "
    "Preserve provenance and distinguish analyst quotations from original reports."
))


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
def factiva_prepare_search(search: api.SearchQuery) -> dict[str, Any]:
    """Preview a search locally, without credentials or a Factiva request."""
    return api.prepare_search(search)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True))
async def factiva_search(search: api.SearchQuery) -> dict[str, Any]:
    """Search via your licensed Retrieval API account. May incur contracted usage."""
    try:
        return await api.search_factiva(search)
    except api.FactivaError as error:
        raise ToolError(str(error)) from None


if __name__ == "__main__":
    mcp.run(transport="stdio")
