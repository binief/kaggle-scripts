# A gateway with no MCPs and no llama: just two plain HTTP services.
import subprocess, sys
subprocess.run("git -C /kaggle/working/cellkit pull -q 2>/dev/null || git clone -q "
               "https://github.com/binief/kaggle-scripts.git /kaggle/working/cellkit", shell=True)
sys.path.insert(0, "/kaggle/working/cellkit")
import cellkit as ck

ck.nginx(port=8890, services=[
    ck.http("/ui", 3000, token="auto"),      # a web app, gated
    ck.exact("/ping", 9000),                 # a health probe, open
])
