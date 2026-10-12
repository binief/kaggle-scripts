#!/usr/bin/env node
/**
 * Minimal stdio MCP server used by the tests.
 * Newline-delimited JSON-RPC 2.0 on stdin/stdout -- enough for mcp-proxy
 * (and any MCP client) to initialize, list tools and call one.
 */
const readline = require("readline");

const SERVER_INFO = { name: "mock-stdio-mcp", version: "9.9.9" };

const TOOLS = [{
  name: "mock_echo",
  description: "Echoes back whatever you pass in `text`.",
  inputSchema: {
    type: "object",
    properties: { text: { type: "string" } },
    required: ["text"],
  },
}];

const send = (msg) => process.stdout.write(JSON.stringify(msg) + "\n");

const handle = (req) => {
  const { id, method, params } = req;
  switch (method) {
    case "initialize":
      return send({ jsonrpc: "2.0", id, result: {
        protocolVersion: params?.protocolVersion || "2025-06-18",
        capabilities: { tools: { listChanged: false } },
        serverInfo: SERVER_INFO,
      }});
    case "notifications/initialized":
    case "notifications/cancelled":
      return;                                   // notifications: no reply
    case "ping":
      return send({ jsonrpc: "2.0", id, result: {} });
    case "tools/list":
      return send({ jsonrpc: "2.0", id, result: { tools: TOOLS } });
    case "tools/call": {
      const args = params?.arguments || {};
      return send({ jsonrpc: "2.0", id, result: {
        content: [{ type: "text", text: `pong:${args.text ?? ""}` }],
        isError: false,
      }});
    }
    default:
      if (id === undefined) return;
      return send({ jsonrpc: "2.0", id, error: { code: -32601, message: `Method not found: ${method}` } });
  }
};

readline.createInterface({ input: process.stdin, terminal: false })
  .on("line", (line) => {
    const t = line.trim();
    if (!t) return;
    try { handle(JSON.parse(t)); }
    catch (e) { process.stderr.write(`[mock-mcp] bad json: ${e.message}\n`); }
  });

process.stderr.write("[mock-mcp] stdio server ready\n");
