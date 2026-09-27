from mcp_server import MCPServer

class MCPClient:
    """Local MCP tool client used by the prototype UI.
    The same tool contracts can later be exposed over stdio/HTTP MCP transport.
    """
    def __init__(self, database_url):
        self.server=MCPServer(database_url)
    async def call_tool(self,name,arguments):
        return self.server.call_tool(name,arguments)
    def register(self, source_id, parsed):
        return self.server.register_file(source_id,parsed)
    def get_server(self):
        return self.server
