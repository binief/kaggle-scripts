# ============================================================
# CELL B -- the MCP servers (same list CELL A mounted; picked up from state)
# ============================================================
import subprocess, sys
subprocess.run("git -C /kaggle/working/cellkit pull -q 2>/dev/null || "
               "git clone -q https://github.com/binief/kaggle-scripts.git /kaggle/working/cellkit",
               shell=True)
sys.path.insert(0, "/kaggle/working/cellkit")
import cellkit as ck

ck.mcps()          # or: ck.mcps(MCPS)
