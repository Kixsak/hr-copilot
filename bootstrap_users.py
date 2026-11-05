"""系統初始化：建立已啟用的帳號（需要主機檔案權限，不經過 UI；交付檔不含任何帳號或密碼）。

R1–R3 綁定的主管紀錄依組織動態找出（不寫死員工編號）；R1–R3 需先完成清理（work/active.csv）。
密碼從 HRC_NEW_PASSWORD 或互動輸入取得，只以 PBKDF2 雜湊存入 users.json。

用法：
  python bootstrap_users.py admin R5
  python bootstrap_users.py group_head R1
  python bootstrap_users.py bg1_head R2 --org BG-1
  python bootstrap_users.py bu01_head R3 --org BG-1/BU-01
  python bootstrap_users.py hrbp_bu07 R4 --org BG-3/BU-07
"""
import argparse
import getpass
import os
import sys

import pandas as pd

import auth

ORG = auth.ORG


def find_head(path: list[str]) -> str:
    df = pd.read_csv(auth._active_csv(), dtype=str, keep_default_na=False, usecols=["EmployeeID", "IsManager", *ORG])
    m = (df["IsManager"] == "Y") & (df["Level1"] != "")
    for lvl, val in zip(ORG[1:], path):
        m &= df[lvl] == val
    for lvl in ORG[len(path) + 1:]:
        m &= df[lvl] == ""
    ids = df.loc[m, "EmployeeID"].tolist()
    if len(ids) != 1:
        raise SystemExit(f"找不到唯一的主管紀錄（{'/'.join(path) or '集團'}：{len(ids)} 筆）")
    return ids[0]


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("username")
    ap.add_argument("role", choices=list(auth.ROLE_NAMES))
    ap.add_argument("--org", default="", help="R2：BG；R3、R4：BG/BU")
    a = ap.parse_args()
    org = [x for x in a.org.split("/") if x]
    pw = os.environ.get("HRC_NEW_PASSWORD") or getpass.getpass("新密碼（至少 8 碼）：")
    if len(pw) < 8:
        raise SystemExit("密碼至少 8 碼")
    if a.role == "R4":
        if len(org) != 2:
            raise SystemExit("R4 需要 --org BG/BU")
        auth.bootstrap_user(a.username, pw, "R4", assigned_bg=org[0], assigned_bu=org[1])
    elif a.role in ("R1", "R2", "R3"):
        need = {"R1": 0, "R2": 1, "R3": 2}[a.role]
        if len(org) != need:
            raise SystemExit(f"{a.role} 的 --org 需要 {need} 層（例如 R2：BG-1；R3：BG-1/BU-01）")
        auth.bootstrap_user(a.username, pw, a.role, employee_id=find_head(org))
    else:
        auth.bootstrap_user(a.username, pw, "R5")
    print(f"已建立並啟用 {a.username}（{a.role}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
