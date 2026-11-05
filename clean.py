"""資料抽取與清理（只能由 R5 啟動；R5 只看得到統計數字，看不到資料內容）。

依本專案的資料規則清理兩個檔案，輸出：
- <clean_dir>/active.csv、<clean_dir>/inactive.csv：清理後資料
- <clean_dir>/pending_review.json：系統無法判定、需 R4 HRBP 確認的項目
- <clean_dir>/cleaning_report.md：清理報告（只含問題類型與筆數，不含個人資料）
"""
from __future__ import annotations

import json
import re
import unicodedata
from collections import Counter
from pathlib import Path

import pandas as pd

import review

BASE_DATE = pd.Timestamp("2026-02-28")
COLS = ["EmployeeID", "EmpStatus", "Level1", "Level2", "Level3", "Level4", "Level5", "Level6", "Level7",
        "BirthDate", "EduLevel", "EduSchool", "Seniority", "Gender", "JobGrade", "IsManager", "Function",
        "DL_IDL", "WorkLocation", "PerfRating"]
TERM_COLS = ["TermDate", "TermType", "TermReason"]

BG_OF_BU = {"BU-01": "BG-1", "BU-02": "BG-1", "BU-03": "BG-1", "BU-04": "BG-2", "BU-05": "BG-2",
            "BU-06": "BG-2", "BU-07": "BG-3", "BU-08": "BG-3"}
FUNC_KEYWORD = {"研發": "RD", "製造": "製造", "品保": "品保", "採購": "採購", "業務": "Sales",
                "人資": "HR", "財務": "財務", "經營企劃": "經營管理"}
EDU = {"高中": "高中", "高職": "高中", "高中職": "高中", "high school": "高中",
       "專科": "專科", "專科學校": "專科", "associate": "專科",
       "學士": "學士", "學士學位": "學士", "大學": "學士", "bachelor": "學士",
       "碩士": "碩士", "碩士學位": "碩士", "ms": "碩士", "master": "碩士",
       "博士": "博士", "博士學位": "博士", "phd": "博士"}
GENDER = {"m": "M", "male": "M", "男": "M", "f": "F", "female": "F", "女": "F"}
YN = {"y": "Y", "yes": "Y", "true": "Y", "1": "Y", "是": "Y",
      "n": "N", "no": "N", "false": "N", "0": "N", "否": "N"}
FUNCS = {"rd": "RD", "sales": "Sales", "hr": "HR"}
VALID_FUNCS = set(FUNC_KEYWORD.values())
CITY = {"台北": "台灣-台北", "taipei": "台灣-台北", "桃園": "台灣-桃園", "taoyuan": "台灣-桃園",
        "高雄": "台灣-高雄", "kaohsiung": "台灣-高雄", "東莞": "中國-東莞", "dongguan": "中國-東莞",
        "蘇州": "中國-蘇州", "suzhou": "中國-蘇州", "曼谷": "泰國-曼谷", "bangkok": "泰國-曼谷",
        "北寧": "越南-北寧", "bac ninh": "越南-北寧", "蒂華納": "墨西哥-蒂華納", "tijuana": "墨西哥-蒂華納",
        "聖荷西": "美國-聖荷西", "san jose": "美國-聖荷西"}
TERM_TYPES = {"自願", "非自願", "退休"}
DATE_FORMATS = ["%Y-%m-%d", "%Y/%m/%d", "%Y%m%d", "%d-%b-%Y"]


def norm(s: str) -> str:
    """全形轉半形、臺→台、去前後空白、多個空白合併為一個。"""
    s = unicodedata.normalize("NFKC", s).replace("臺", "台")
    return re.sub(r"\s+", " ", s).strip()


def parse_date(s: str) -> pd.Timestamp | None:
    for fmt in DATE_FORMATS:
        try:
            return pd.Timestamp(pd.to_datetime(s, format=fmt))
        except (ValueError, TypeError):
            continue
    return None


def map_loc(s: str) -> str | None:
    low = s.lower()
    hits = {v for k, v in CITY.items() if k in low}
    return hits.pop() if len(hits) == 1 else None


class Cleaner:
    def __init__(self) -> None:
        self.issues: Counter = Counter()   # (檔案, 問題, 處理方式) -> 筆數
        self.pending: list[dict] = []

    def log(self, src: str, issue: str, action: str, n: int = 1) -> None:
        if n:
            self.issues[(src, issue, action)] += n

    def add_pending(self, src: str, row: pd.Series, field: str, issue: str, current: str, proposed: str,
                    reviewers: list[dict], path: dict, rh: str = "") -> None:
        self.pending.append(review.new_item(src, row, field, issue, current, proposed, reviewers, path, rh))

    def clean_file(self, path: Path, src: str, cols: list[str]) -> pd.DataFrame:
        df = pd.read_csv(path, dtype=str, keep_default_na=False, encoding="utf-8-sig")
        n0 = len(df)
        hdr = df["EmployeeID"].str.strip() == "EmployeeID"
        self.log(src, "檔案中間夾帶重複的表頭列", "刪除", int(hdr.sum()))
        df = df[~hdr]
        blank = (df[cols] == "").all(axis=1)
        self.log(src, "整列空白", "刪除", int(blank.sum()))
        df = df[~blank]
        dup = df.duplicated()
        self.log(src, "所有欄位完全相同的重複列", "保留一筆、刪除其餘", int(dup.sum()))
        df = df[~dup].copy()

        before = df.copy()
        for c in df.columns:
            df[c] = df[c].map(norm)
        for c in df.columns:
            n = int((before[c] != df[c]).sum())
            self.log(src, f"{c}：全形字元／前後空白／臺台異體", "正規化", n)

        self._fix_values(df, src)
        self._fix_org(df, src)
        if src == "inactive":
            self._fix_term(df, src)
        dup2 = df.duplicated()
        self.log(src, "正規化後所有欄位完全相同的重複列", "保留一筆、刪除其餘", int(dup2.sum()))
        df = df[~dup2].copy()
        self._derive(df, src)
        dup_id =df["EmployeeID"].duplicated(keep=False)
        if dup_id.any():
            self.log(src, "EmployeeID 重複但內容不同", "列入待確認；未核准前該編號所有列都不納入", int(dup_id.sum()))
            for _, r in df[dup_id].iterrows():
                self.add_pending(src, r, "_row", "同一員工編號有多筆不同資料", "", "保留此筆", [_req(r)], _req(r),
                                 review.row_hash(r))
        self.log(src, "原始列數", "-", n0)
        self.log(src, "清理後列數", "-", len(df))
        return df

    def _map_field(self, df: pd.DataFrame, src: str, col: str, mapping: dict, valid: set[str]) -> None:
        def f(v: str) -> str:
            if v in valid or v == "":
                return v
            return mapping.get(v.lower(), mapping.get(v, ""))
        new = df[col].map(f)
        changed = (new != df[col])
        fixed = changed & (new != "")
        bad = changed & (new == "")
        self.log(src, f"{col}：非標準寫法（如 {', '.join(df.loc[fixed, col].value_counts().index[:4])}）", "對應為標準值", int(fixed.sum()))
        self.log(src, f"{col}：無法判定的值", "視為缺值", int(bad.sum()))
        df[col] = new

    def _fix_values(self, df: pd.DataFrame, src: str) -> None:
        # 在職狀態：依檔案來源判定（離職檔一律 Inactive；在職檔為截至基準日的在職員工）
        want = "Active" if src == "active" else "Inactive"
        wrong = df["EmpStatus"] != want
        self.log(src, f"EmpStatus 不是 {want}（大小寫、空白或錯誤狀態）", f"依檔案來源設為 {want}", int(wrong.sum()))
        df["EmpStatus"] = want

        self._map_field(df, src, "EduLevel", EDU, set(EDU.values()))
        self._map_field(df, src, "Gender", GENDER, {"M", "F"})
        self._map_field(df, src, "IsManager", YN, {"Y", "N"})
        self._map_field(df, src, "Function", FUNCS, VALID_FUNCS)
        df["DL_IDL"] = df["DL_IDL"].str.upper()

        loc_ok = set(CITY.values())
        new = df["WorkLocation"].map(lambda v: v if v in loc_ok or v == "" else (map_loc(v) or ""))
        self.log(src, "WorkLocation：非標準寫法（英文、空白分隔、只寫城市）", "對應為標準值",
                 int(((new != df["WorkLocation"]) & (new != "")).sum()))
        self.log(src, "WorkLocation：無法判定", "視為缺值", int(((new != df["WorkLocation"]) & (new == "")).sum()))
        df["WorkLocation"] = new

        jg = df["JobGrade"].str.upper().str.replace(" ", "", regex=False)
        jg = jg.map(lambda v: f"G{int(v[1:]):02d}" if re.fullmatch(r"G\d{1,2}", v) else v)
        ok = jg.str.fullmatch(r"G(0[89]|1[0-6])")
        self.log(src, "JobGrade：非標準寫法（G8、G 08、g08）", "補成 G 加兩位數", int(((jg != df["JobGrade"]) & ok).sum()))
        self.log(src, "JobGrade：不在 G08–G16", "視為缺值", int((~ok & (jg != "")).sum()))
        df["JobGrade"] = jg.where(ok, "")

        bd = df["BirthDate"].map(lambda v: parse_date(v) if v else None)
        std = df["BirthDate"].str.fullmatch(r"\d{4}-\d{2}-\d{2}")
        self.log(src, "BirthDate：非 YYYY-MM-DD（YYYY/MM/DD、YYYYMMDD、DD-Mon-YYYY）", "轉為 YYYY-MM-DD",
                 int((~std & bd.notna()).sum()))
        self.log(src, "BirthDate：無法解析", "視為缺值", int((bd.isna() & (df["BirthDate"] != "")).sum()))
        age = bd.map(lambda d: _age(d) if d is not None else None)
        out = age.map(lambda a: a is not None and not (18 <= a <= 70))
        self.log(src, "年齡（以基準日計算）不在 18–70 歲", "BirthDate 視為缺值", int(out.sum()))
        df["BirthDate"] = [d.strftime("%Y-%m-%d") if d is not None and not o else "" for d, o in zip(bd, out)]

        num = df["Seniority"].str.extract(r"^(-?\d+(?:\.\d+)?)\s*(?:年|yrs|years)?$")[0]
        sen = pd.to_numeric(num, errors="coerce")
        self.log(src, "Seniority：帶單位文字（年、yrs、years）", "取出數字", int((num.notna() & (num != df["Seniority"])).sum()))
        self.log(src, "Seniority：無法解析", "視為缺值", int((sen.isna() & (df["Seniority"] != "")).sum()))
        self.log(src, "Seniority：負值", "視為缺值", int((sen < 0).sum()))
        sen = sen.where(sen >= 0)
        df["Seniority"] = sen.map(lambda v: f"{v:.2f}" if pd.notna(v) else "")

        bad_pr = (df["PerfRating"] != "") & (sen < 1)
        self.log(src, "PerfRating：年資未滿 1 年卻有績效（違反資料規則）", "PerfRating 視為缺值", int(bad_pr.sum()))
        df.loc[bad_pr, "PerfRating"] = ""

    def _fix_org(self, df: pd.DataFrame, src: str) -> None:
        df["Level3"] = df["Level3"].str.upper().str.replace(" ", "", regex=False)
        df.loc[~df["Level3"].isin(list(BG_OF_BU) + [""]), "Level3"] = ""
        # Level3 空白但 Level4 有值：依命名規則（Level4 = BU 代碼＋空白＋處名稱）取得 BU
        prefix = df["Level4"].str.extract(r"^(BU-0[1-8]) ")[0].fillna("")
        fill = (df["Level3"] == "") & (prefix != "")
        self.log(src, "Level3 空白，但 Level4 名稱含 BU 代碼", "依命名規則由 Level4 取得 BU", int(fill.sum()))
        df.loc[fill, "Level3"] = prefix[fill]
        conflict = (df["Level3"] != "") & (prefix != "") & (prefix != df["Level3"])
        self.log(src, "Level3 與 Level4 名稱中的 BU 代碼不一致", "列入待 R4 確認，確認前 Level3 視為缺值", int(conflict.sum()))
        for _, r in df[conflict].iterrows():
            self.add_pending(src, r, "Level3", f"Level3={r['Level3']} 與 Level4={r['Level4']} 不一致", r["Level3"],
                             prefix[_], [{"Level3": r["Level3"]}, {"Level3": prefix[_]}],
                             {"Level2": r["Level2"], "Level3": prefix[_]})
        df.loc[conflict, "Level3"] = ""

        bg = df["Level3"].map(BG_OF_BU).fillna("")
        fill = (df["Level2"] == "") & (bg != "")
        self.log(src, "Level2 空白", "依固定的 BU→BG 對應補上", int(fill.sum()))
        df.loc[fill, "Level2"] = bg[fill]
        bad = (bg != "") & (df["Level2"] != bg)
        self.log(src, "Level2 與 BU→BG 固定對應不符", "列入待 R4 確認，確認前 Level2 視為缺值", int(bad.sum()))
        for _, r in df[bad].iterrows():  # 兩方審核：該 BU（建議 BG 下）的 HRBP ＋ 原始標記 BG 的 HRBP
            self.add_pending(src, r, "Level2", f"Level2={r['Level2']} 與 {r['Level3']} 應屬 {bg[_]} 不符", r["Level2"],
                             bg[_], [{"Level2": bg[_], "Level3": r["Level3"]}, {"Level2": r["Level2"]}],
                             {"Level2": bg[_], "Level3": r["Level3"]})
        df.loc[bad, "Level2"] = ""

        # Function：處名稱含職能關鍵字；集團/BG/BU 主管為經營管理
        kw = df["Level4"].map(_func_from_name)
        top_mgr = (df["IsManager"] == "Y") & (df["Level4"] == "")
        expected = kw.where(df["Level4"] != "", top_mgr.map(lambda t: "經營管理" if t else ""))
        fill = (df["Function"] == "") & (expected != "")
        self.log(src, "Function 空白", "依 Level4 處名稱關鍵字（或高階主管＝經營管理）判定", int(fill.sum()))
        df.loc[fill, "Function"] = expected[fill]
        conflict = (df["Function"] != "") & (expected != "") & (df["Function"] != expected)
        self.log(src, "Function 與處名稱關鍵字不一致", "列入待 R4 確認，確認前 Function 視為缺值", int(conflict.sum()))
        for _, r in df[conflict].iterrows():
            self.add_pending(src, r, "Function", f"Function={r['Function']} 與 {r['Level4']} 不一致", r["Function"],
                             expected[_], [_req(r)], _req(r))
        df.loc[conflict, "Function"] = ""

        # 主管一律 IDL
        dl = df["DL_IDL"]
        fix = (df["IsManager"] == "Y") & (dl != "IDL")
        self.log(src, "主管的 DL_IDL 空白或不是 IDL", "主管一律設為 IDL", int(fix.sum()))
        df.loc[fix, "DL_IDL"] = "IDL"
        bad = ~df["DL_IDL"].isin(["DL", "IDL"])
        self.log(src, "DL_IDL 空白或不合法（非主管）", "視為缺值", int(bad.sum()))
        df.loc[bad, "DL_IDL"] = ""

    def _fix_term(self, df: pd.DataFrame, src: str) -> None:
        td = df["TermDate"].map(lambda v: parse_date(v) if v else None)
        std = df["TermDate"].str.fullmatch(r"\d{4}-\d{2}-\d{2}")
        self.log(src, "TermDate：非 YYYY-MM-DD", "轉為 YYYY-MM-DD", int((~std & td.notna()).sum()))
        self.log(src, "TermDate：無法解析", "視為缺值", int((td.isna()).sum()))
        df["TermDate"] = td.map(lambda d: d.strftime("%Y-%m-%d") if d is not None else "")
        bad = ~df["TermType"].isin(TERM_TYPES)
        self.log(src, "TermType 空白或不合法", "視為缺值", int(bad.sum()))
        df.loc[bad, "TermType"] = ""

    def _derive(self, df: pd.DataFrame, src: str) -> None:
        missing = {c: int((df[c] == "").sum()) for c in ["Level3", "BirthDate", "Seniority", "Gender",
                                                        "IsManager", "Function", "DL_IDL", "EduLevel"]}
        for c, n in missing.items():
            self.log(src, f"清理後仍缺值：{c}（計算用到此欄位時排除並說明筆數）", "保留為缺值", n)


def _req(r: pd.Series) -> dict:
    return {k: r[k] for k in ("Level2", "Level3") if r[k]}


def _age(d: pd.Timestamp) -> int:
    return BASE_DATE.year - d.year - ((BASE_DATE.month, BASE_DATE.day) < (d.month, d.day))


def _func_from_name(name: str) -> str:
    for k, v in FUNC_KEYWORD.items():
        if k in name:
            return v
    return ""


def write_report(c: Cleaner, path: Path) -> None:
    lines = ["# 資料清理報告", "", "資料基準日：2026-02-28。"
             "無法依資料規則判定的值一律視為缺值，不推估、不補值；系統無法判定的衝突列入待 R4 HRBP 確認。", "",
             "| 檔案 | 問題 | 影響筆數 | 處理方式 |", "|---|---|---|---|"]
    for (src, issue, action), n in c.issues.items():
        lines.append(f"| {src} | {issue} | {n} | {action} |")
    st = Counter((x["field"], review.status(x)) for x in c.pending)
    lines += ["", f"待 R4 確認項目：{len(c.pending)} 筆（明細僅負責的 R4 HRBP 可在審核頁查看）。", "",
              "| 類型 | 狀態 | 筆數 |", "|---|---|---|"] + [f"| {f} | {s} | {n} |" for (f, s), n in sorted(st.items())]
    lines += ["", "規則：所有審核方都核准才生效；任一方駁回或尚未決定時，該欄位維持缺值、相關資料不納入"
              "任何非 R1 的資料範圍（fail-closed）。", ""]
    path.write_text("\n".join(lines), encoding="utf-8")


def run(data_dir: Path, out_dir: Path, user) -> dict:
    if user is None or user.role != "R5" or not user.active:
        raise PermissionError("只有 R5 系統管理員可以啟動資料清理")
    out_dir.mkdir(parents=True, exist_ok=True)
    c = Cleaner()
    a = c.clean_file(data_dir / "employees_active.csv", "active", COLS)
    i = c.clean_file(data_dir / "employees_inactive.csv", "inactive", COLS + TERM_COLS)
    both = set(a["EmployeeID"]) & set(i["EmployeeID"])
    c.log("both", "同一員工同時出現在在職與離職檔", "列入待 R4 確認；未核准前兩檔都不納入", len(both))
    for eid in both:
        r = a[a["EmployeeID"] == eid].iloc[0]
        c.add_pending("both", r, "EmpStatus", "同時在在職與離職檔", "Active", "Inactive", [_req(r)], _req(r))
    a.to_csv(out_dir / "active.csv", index=False, encoding="utf-8")
    i.to_csv(out_dir / "inactive.csv", index=False, encoding="utf-8")
    old = {}
    p = out_dir / "pending_review.json"
    if p.exists():  # 保留 R4 已做過的決定；id 含原值與建議值的雜湊，資料變了就不會沿用舊決定
        old = {x["id"]: x for x in json.loads(p.read_text(encoding="utf-8"))}
    for x in c.pending:
        o = old.get(x["id"])
        if o and o.get("approvals"):
            x["approvals"], x["history"] = o["approvals"], o.get("history", [])
        x["status"] = review.status(x)
    p.write_text(json.dumps(c.pending, ensure_ascii=False, indent=1), encoding="utf-8")
    write_report(c, out_dir / "cleaning_report.md")
    return {"active_rows": len(a), "inactive_rows": len(i), "pending": len(c.pending),
            "issue_types": len(c.issues)}


def main() -> int:
    """命令列啟動清理：python clean.py（需以 R5 帳號登入；只輸出統計數字）。"""
    import getpass
    import os
    import sys

    from auth import login
    from query import _clean_dir, audit

    sys.stdout.reconfigure(encoding="utf-8")
    name = os.environ.get("HRC_ADMIN_USER") or input("帳號：")
    pw = os.environ.get("HRC_ADMIN_PASSWORD") or getpass.getpass("密碼：")
    user = login(name, pw)
    if not user or user.role != "R5":
        audit({"user": name, "event": "clean", "status": "denied"})
        print("只有 R5 系統管理員可以啟動資料清理。")
        return 1
    res = run(Path(os.environ.get("HRC_DATA_DIR", "data")), _clean_dir(), user)
    audit({"user": name, "role": "R5", "event": "clean", "status": "ok", "result": res})
    print(res)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
