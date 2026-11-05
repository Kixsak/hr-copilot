"""合成測試資料（不使用任何真實員工資料）。每個測試取得獨立的資料、帳號與紀錄目錄。"""
import json
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

COLS = ["EmployeeID", "EmpStatus", "Level1", "Level2", "Level3", "Level4", "Level5", "Level6", "Level7",
        "BirthDate", "EduLevel", "EduSchool", "Seniority", "Gender", "JobGrade", "IsManager", "Function",
        "DL_IDL", "WorkLocation", "PerfRating"]
TEAM = ("BU-X 研發處", "BU-X 研發一部", "BU-X 研發一部 第1課", "BU-X 研發一部 第1課 第1組")
PASSWORD = "Passw0rd!"


def row(eid, l2="", l3="", org=(), bd="1980-06-01", sen="5.00", mgr="N", **kw):
    r = dict.fromkeys(COLS, "")
    levels = list(org) + [""] * (4 - len(org))
    r.update(EmployeeID=eid, EmpStatus="Active", Level1="Group", Level2=l2, Level3=l3, Level4=levels[0],
             Level5=levels[1], Level6=levels[2], Level7=levels[3], BirthDate=bd, EduLevel="學士", Seniority=sen,
             Gender="M", JobGrade="G10", IsManager=mgr, Function="RD", DL_IDL="IDL", WorkLocation="台灣-台北",
             PerfRating="B")
    r.update(kw)
    return r


def leaver(eid, l2, l3, date="2026-02-10", ttype="自願"):
    return {**row(eid, l2, l3, TEAM), "EmpStatus": "Inactive", "TermDate": date, "TermType": ttype,
            "TermReason": "薪資福利"}


def build_active():
    rows = [row("E000001", mgr="Y", Function="經營管理"),                       # 集團主管（R1）
            row("E000002", "BG-1", mgr="Y", Function="經營管理"),               # BG-1 主管（R2）
            row("E000003", "BG-1", "BU-X", mgr="Y", Function="經營管理")]       # BG-1 / BU-X 主管（R3）
    # 未滿 35、非主管、IDL、RD 的組合；其中 2 筆年資缺值
    for i, sen in enumerate(["1.00", "2.00", "3.00", "4.00", "5.00", "6.00", "7.00", "8.00", "", ""]):
        rows.append(row(f"E0001{i:02d}", "BG-1", "BU-X", TEAM, bd="1995-05-05", sen=sen))
    rows.append(row("E000120", "BG-1", "BU-X", TEAM, bd="1991-02-28", sen="3.00"))   # 基準日當天滿 35
    rows.append(row("E000121", "BG-1", "BU-X", TEAM, bd="1991-03-01", sen="3.00"))   # 34 歲
    # 單人小組的主管（小樣本）
    rows.append(row("E000130", "BG-1", "BU-X", TEAM[:3] + ("BU-X 研發一部 第1課 第9組",), mgr="Y", PerfRating="A"))
    # 另一個 BG 底下的同名 BU
    rows += [row(f"E0002{i:02d}", "BG-2", "BU-X", TEAM) for i in range(6)]
    # Level2 待確認（原始為 BG-2，建議 BG-1）
    rows.append(row("E000300", "", "BU-X", TEAM))
    # 同時出現在離職檔
    rows.append(row("E000400", "BG-1", "BU-X", TEAM))
    # 重複員工編號、內容不同
    rows += [row("E000500", "BG-1", "BU-X", TEAM, sen="1.00"), row("E000500", "BG-1", "BU-X", TEAM, sen="9.00")]
    return pd.DataFrame(rows)


def build_leavers():
    rows = [leaver(f"E0006{i:02d}", "BG-1", "BU-X") for i in range(6)]
    rows.append(leaver("E000610", "BG-1", "BU-X", date="2026-01-15"))   # 不在當月
    rows.append(leaver("E000400", "BG-1", "BU-X"))
    rows += [leaver(f"E0007{i:02d}", "BG-2", "BU-X") for i in range(6)]
    rows += [leaver(f"E0008{i:02d}", "BG-1", "BU-Y") for i in range(6)]  # BU-Y 無在職者
    return pd.DataFrame(rows)


def row_hash(df, eid, sen):
    from review import row_hash as rh
    r = df[(df.EmployeeID == eid) & (df.Seniority == sen)].iloc[0]
    return rh(r)


@pytest.fixture
def env(tmp_path, monkeypatch):
    clean_dir, logs = tmp_path / "work", tmp_path / "logs"
    clean_dir.mkdir()
    monkeypatch.setenv("HRC_CLEAN_DIR", str(clean_dir))
    monkeypatch.setenv("HRC_LOG_DIR", str(logs))
    monkeypatch.setenv("HRC_USERS_FILE", str(tmp_path / "users.json"))
    import review
    a, lv = build_active(), build_leavers()
    a.to_csv(clean_dir / "active.csv", index=False)
    lv.to_csv(clean_dir / "inactive.csv", index=False)
    items = [
        review.new_item("active", a[a.EmployeeID == "E000300"].iloc[0], "Level2", "Level2 與 BU 不符", "BG-2", "BG-1",
                        [{"Level2": "BG-1", "Level3": "BU-X"}, {"Level2": "BG-2"}],
                        {"Level2": "BG-1", "Level3": "BU-X"}),
        review.new_item("both", a[a.EmployeeID == "E000400"].iloc[0], "EmpStatus", "同時在兩檔", "Active", "Inactive",
                        [{"Level2": "BG-1", "Level3": "BU-X"}], {"Level2": "BG-1", "Level3": "BU-X"}),
    ]
    for sen in ("1.00", "9.00"):
        r = a[(a.EmployeeID == "E000500") & (a.Seniority == sen)].iloc[0]
        items.append(review.new_item("active", r, "_row", "同一員工編號多筆", "", "保留此筆",
                                     [{"Level2": "BG-1", "Level3": "BU-X"}], {"Level2": "BG-1", "Level3": "BU-X"},
                                     row_hash=review.row_hash(r)))
    (clean_dir / "pending_review.json").write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
    import auth
    for name, role, kw in [("group_head", "R1", {"employee_id": "E000001"}), ("bg1", "R2", {"employee_id": "E000002"}),
                           ("bux", "R3", {"employee_id": "E000003"}),
                           ("hr1x", "R4", {"assigned_bg": "BG-1", "assigned_bu": "BU-X"}),
                           ("hr2x", "R4", {"assigned_bg": "BG-2", "assigned_bu": "BU-X"}),
                           ("hr1y", "R4", {"assigned_bg": "BG-1", "assigned_bu": "BU-Y"}),
                           ("admin", "R5", {}), ("admin2", "R5", {})]:
        auth.bootstrap_user(name, PASSWORD, role, **kw)
    auth.reset_lockouts()
    return tmp_path


@pytest.fixture
def users(env):
    import auth
    return {n: auth.get_user(n) for n in ("group_head", "bg1", "bux", "hr1x", "hr2x", "hr1y", "admin", "admin2")}
