"""產生完全虛構的示範資料到 data/（固定 seed，可重現）：python generate_demo_data.py

與任何真實員工或真實資料集無關。結構遵循 clean.py 的資料規則（BU→BG 對應、處名稱含職能關鍵字、
主管 IDL、年資 <1 年無績效），並刻意放入少量髒資料，用來展示清理流程與待確認審核。
"""
import random
from pathlib import Path

import pandas as pd

from clean import BASE_DATE, COLS, TERM_COLS

SEED = 20260228
BG_OF_BU = {"BU-01": "BG-1", "BU-02": "BG-1", "BU-03": "BG-1", "BU-04": "BG-2", "BU-05": "BG-2",
            "BU-06": "BG-2", "BU-07": "BG-3", "BU-08": "BG-3"}
MAIN_LOC = {"BU-01": "台灣-台北", "BU-02": "台灣-桃園", "BU-03": "中國-東莞", "BU-04": "中國-蘇州",
            "BU-05": "泰國-曼谷", "BU-06": "越南-北寧", "BU-07": "台灣-高雄", "BU-08": "墨西哥-蒂華納"}
DEPT_FUNC = {"研發": "RD", "製造": "製造", "業務": "Sales", "品保": "品保", "採購": "採購", "人資": "HR", "財務": "財務"}
SCHOOLS = ["Demo University A", "Demo University B", "Demo Institute of Technology", "Demo College"]
REASONS = {"自願": ["薪資福利", "職涯發展", "家庭因素"], "非自願": ["績效不佳", "組織調整"], "退休": ["屆齡退休"]}

rng = random.Random(SEED)
_next_id = 0


def new_id() -> str:
    global _next_id
    _next_id += 1
    return f"DEMO{_next_id:06d}"


def person(org: list[str], func: str, manager: bool, grade: str, loc: str) -> dict:
    """org = [Level2..Level7]（不足補空字串）。"""
    age = min(60.0, max(22.0, rng.gauss(36, 8) + (7 if manager else 0)))
    seniority = round(rng.betavariate(1.5, 3) * (age - 22), 2)
    birth = BASE_DATE - pd.Timedelta(days=int(age * 365.25))
    r = dict.fromkeys(COLS + TERM_COLS, "")
    r.update(EmployeeID=new_id(), EmpStatus="Active", Level1="Group", BirthDate=birth.strftime("%Y-%m-%d"),
             EduLevel=rng.choices(["高中", "專科", "學士", "碩士", "博士"],
                                  [8, 12, 50, 27 if func != "RD" else 35, 3])[0],
             EduSchool=rng.choice(SCHOOLS), Seniority=f"{seniority:.2f}", Gender=rng.choice("MMF"),
             JobGrade=grade, IsManager="Y" if manager else "N", Function=func,
             DL_IDL="DL" if (func == "製造" and not manager and rng.random() < 0.7) else "IDL",
             WorkLocation=loc if rng.random() < 0.8 else rng.choice(list(MAIN_LOC.values())),
             PerfRating=rng.choices("ABCD", [15, 50, 28, 7])[0] if seniority >= 1 else "")
    for i, v in enumerate(org):
        r[f"Level{i + 2}"] = v
    return r


def build() -> tuple[pd.DataFrame, pd.DataFrame]:
    active, staff_orgs = [person([], "經營管理", True, "G16", "台灣-台北")], []
    for bg in sorted(set(BG_OF_BU.values())):
        active.append(person([bg], "經營管理", True, "G16", "台灣-台北"))
    for bu, bg in BG_OF_BU.items():
        active.append(person([bg, bu], "經營管理", True, "G15", MAIN_LOC[bu]))
        depts = ["研發", "製造", "業務"] + rng.sample(["品保", "採購", "人資", "財務"], 2)
        for d in depts:
            l4, f = f"{bu} {d}處", DEPT_FUNC[d]
            active.append(person([bg, bu, l4], f, True, "G14", MAIN_LOC[bu]))
            for b in ("一部", "二部"):
                l5 = f"{bu} {d}{b}"
                active.append(person([bg, bu, l4, l5], f, True, "G13", MAIN_LOC[bu]))
                for k in (1, 2):
                    l6 = f"{l5} 第{k}課"
                    active.append(person([bg, bu, l4, l5, l6], f, True, "G12", MAIN_LOC[bu]))
                    staff_orgs += [([bg, bu, l4, l5, l6], f, MAIN_LOC[bu])] * rng.randint(7, 12)
    for org, f, loc in staff_orgs:
        active.append(person(org, f, False, rng.choices(["G08", "G09", "G10", "G11", "G12"], [10, 30, 30, 20, 10])[0], loc))
    leavers = []
    for org, f, loc in rng.sample(staff_orgs, 60):
        r = person(org, f, False, rng.choice(["G09", "G10", "G11"]), loc)
        ttype = rng.choices(list(REASONS), [70, 20, 10])[0]
        r.update(EmpStatus="Inactive", TermDate=f"2026-02-{rng.randint(1, 27):02d}", TermType=ttype,
                 TermReason=rng.choice(REASONS[ttype]))
        leavers.append(r)
    return pd.DataFrame(active)[COLS], pd.DataFrame(leavers)[COLS + TERM_COLS]


def add_dirt(df: pd.DataFrame) -> pd.DataFrame:
    """對少數非主管列放入髒資料（各一筆，clean.py 會自動修正，Level2 不符者進入待確認）。"""
    i = rng.sample(list(df.index[df.IsManager == "N"]), 6)
    df.loc[i[0], "JobGrade"] = "G 9"
    df.loc[i[1], "Seniority"] = f"{df.loc[i[1], 'Seniority']} yrs"
    df.loc[i[2], "BirthDate"] = df.loc[i[2], "BirthDate"].replace("-", "/")
    df.loc[i[3], "EduLevel"] = "大學"
    df.loc[i[4], "Level3"] = ""
    df.loc[i[5], "Level2"] = "BG-3" if df.loc[i[5], "Level2"] != "BG-3" else "BG-1"
    return pd.concat([df, df.loc[[i[0]]]], ignore_index=True)  # 一筆完全重複列


def main() -> None:
    active, leavers = build()
    out = Path(__file__).parent / "data"
    out.mkdir(exist_ok=True)
    add_dirt(active).to_csv(out / "employees_active.csv", index=False, encoding="utf-8-sig")
    leavers.to_csv(out / "employees_inactive.csv", index=False, encoding="utf-8-sig")
    print(f"active={len(active) + 1}, inactive={len(leavers)} -> {out}")


if __name__ == "__main__":
    main()
