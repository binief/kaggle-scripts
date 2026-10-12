# ============================================================
# CELL A -- the gateway: one public port for WebUI + /v1 + all MCPs
# ============================================================
import subprocess, sys
subprocess.run("git -C /kaggle/working/cellkit pull -q 2>/dev/null || "
               "git clone -q https://github.com/binief/kaggle-scripts.git /kaggle/working/cellkit",
               shell=True)
sys.path.insert(0, "/kaggle/working/cellkit")
import cellkit as ck

MCPS = [
    ck.mcp("/mcp", 8891, provider="websearch"),        # clone + build + proxy, handled
    # ck.mcp("/mcp/fs", 8892, npx="@modelcontextprotocol/server-filesystem",
    #        args=["/kaggle/working"], stateless=True),
]

ck.nginx(port=8890, llama=8080, token="auto", services=MCPS)

# open the WebUI at  https://<tunnel>/          (paste the printed /v1 token in
# Settings -> General -> API Key)   |   MCP clients use  https://<tunnel>/mcp
# OpenAI clients: base_url = https://<tunnel>/v1  (llama's /v1 routes pass through
# unchanged -- there is nothing at exactly /v1, that 404 is llama's own)
