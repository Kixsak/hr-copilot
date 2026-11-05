"""容器啟動腳本：第一次啟動時自動初始化，之後直接啟動網頁。

- 有 HRC_BOOTSTRAP_PASSWORD 時：建立 R5 帳號 admin；若尚未清理，以 admin 執行清理（R5 流程）；
  再建立示範帳號（group_head / bg1_head / bu01_head / hrbp_bu07，密碼同上，只存雜湊於 /state）。
- 密碼只從執行時環境變數讀取，不寫入映像或檔案。
- HRC_ENTRY_DRY_RUN=1：只做初始化、不啟動網頁（本機驗證用）。
"""
import os
import subprocess
import sys
from pathlib import Path

import auth

DEMO = [("group_head", "R1", ""), ("bg1_head", "R2", "BG-1"), ("bu01_head", "R3", "BG-1/BU-01"),
        ("hrbp_bu07", "R4", "BG-3/BU-07")]


def run(args: list[str], extra_env: dict) -> None:
    subprocess.run([sys.executable, *args], env={**os.environ, **extra_env}, check=True)


def main() -> None:
    pw = os.environ.get("HRC_BOOTSTRAP_PASSWORD", "")
    clean_dir = Path(os.environ.get("HRC_CLEAN_DIR", "work"))
    if pw:
        if auth.get_user("admin") is None:
            auth.bootstrap_user("admin", pw, "R5")
            print("[init] 已建立 R5 帳號 admin", flush=True)
        if not (clean_dir / "active.csv").exists():
            print("[init] 以 R5 執行資料清理…", flush=True)
            run(["clean.py"], {"HRC_ADMIN_USER": "admin", "HRC_ADMIN_PASSWORD": pw})
        for name, role, org in DEMO:
            if auth.get_user(name) is None:
                run(["bootstrap_users.py", name, role] + (["--org", org] if org else []), {"HRC_NEW_PASSWORD": pw})
    elif auth.get_user("admin") is None:
        print("[init] 未設定 HRC_BOOTSTRAP_PASSWORD，且尚無帳號；請參考 README 的 Quick start。", flush=True)
    if os.environ.get("HRC_ENTRY_DRY_RUN") == "1":
        return
    port = os.environ.get("PORT", "8501")
    os.execv(sys.executable, [sys.executable, "-m", "streamlit", "run", "app.py", "--server.address=0.0.0.0",
                              f"--server.port={port}", "--server.headless=true"])


if __name__ == "__main__":
    main()
