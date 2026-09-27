"""Optional real MCP stdio server exposing the same read-only tools.
Run: python mcp_stdio_server.py
"""
from pathlib import Path
from mcp.server.fastmcp import FastMCP
from mcp_server import MCPServer
from source_loader import check_allowed_path, read_file
import os

mcp=FastMCP("Business Data Analyst MCP")
server=MCPServer(os.getenv("DATABASE_URL","sqlite:///demo.db"))

@mcp.tool()
def get_database_schema():
    """Tables and columns of the configured SQL database."""
    return server.get_database_schema()

@mcp.tool()
def aggregate_data(table, metric, aggregation="sum", group_by=None, filters=None, limit=500, date_column=None, date_grain=None, date_from=None, date_to=None):
    """Aggregate a SQL database table."""
    return server.aggregate_data(table,metric,aggregation,group_by,filters,limit,date_column,date_grain,date_from,date_to)

@mcp.tool()
def register_google_sheet(source_id, url):
    """Load every tab of a public Google Sheet as a source."""
    return server.register_google_sheet(source_id,url)

@mcp.tool()
def load_file(source_id: str, path: str):
    """Load a local CSV/XLSX/XLS/PDF/DOCX/PPTX/TXT/MD/JSON/HTML file as a source."""
    p=check_allowed_path(path)
    return server.register_file(source_id,read_file(p.name,p.read_bytes()))

@mcp.tool()
def register_web(source_id: str, url: str):
    """Load a web page (text and HTML tables) as a source."""
    return server.register_web(source_id,url)

@mcp.tool()
def register_database(source_id: str, url: str, max_rows: int = 200000):
    """Load every table of a SQL database (SQLAlchemy URL, read-only SELECTs) as a source; tables act like sheets."""
    return server.register_database(source_id, url, max_rows)

@mcp.tool()
def source_schema(source_id):
    """Columns, roles and sample values of a loaded source."""
    return server.source_schema(source_id)

@mcp.tool()
def aggregate_source(source_id: str, metric: str, sheet_name: str | None = None, aggregation: str = "sum", group_by: list[str] | None = None, filters: list[dict] | None = None, limit: int = 500, date_column: str | None = None, date_grain: str | None = None, date_from: str | None = None, date_to: str | None = None, sort: str | None = None, top_n: int | None = None):
    """Sum/avg/count/min/max a metric, optionally grouped. filters: [{column, op (eq|ne|gt|gte|lt|lte|contains|in|not_in), value}]. sort asc|desc with top_n for rankings."""
    return server.call_tool("aggregate_source", locals())

@mcp.tool()
def query_source(source_id: str, sheet_name: str | None = None, columns: list[str] | None = None, filters: list[dict] | None = None, limit: int = 500, sort_by: str | None = None, sort: str | None = None):
    """Return matching rows. Same filter format as aggregate_source."""
    return server.call_tool("query_source", locals())

@mcp.tool()
def distinct_values(source_id: str, column: str, sheet_name: str | None = None, limit: int = 50):
    """Real values of a column with row counts, to match user wording onto actual data."""
    return server.call_tool("distinct_values", locals())

@mcp.tool()
def find_images(source_id: str, query: str = "", limit: int = 6):
    """Images (alt text + absolute URL) on a web page source that match the question."""
    return server.call_tool("find_images", locals())

@mcp.tool()
def find_videos(source_id: str, query: str = "", limit: int = 8):
    """Videos (title + link: YouTube or mp4) on a web page source that match the question."""
    return server.call_tool("find_videos", locals())

@mcp.tool()
def search_source(source_id, query, max_chars=18000):
    """Keyword search over a document or web page source."""
    return server.search_text(source_id,query,max_chars)

if __name__ == "__main__":
    mcp.run(transport="stdio")
