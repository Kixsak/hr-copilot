"""權限過濾 + 決定性計算引擎。LLM 只能透過這裡取得資料，所有數字都由這裡算出。

CLI（Skill 用）：HRC_USER_ID=<帳號> python query.py --spec '<JSON>'
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
import time
from functools import lru_cache
from pathlib import Path
from typing import Literal, Optional, Union

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, ValidationError

import review
from auth import User

ROOT = Path(__file__).parent
BASE_DATE = pd.Timestamp("2026-02-28")
PERIOD_START = pd.Timestamp("2026-02-01")  # 「當月」= 資料基準月
K = 5  # 只能看統計的角色：人數 1–4 的格子不顯示（使用者決策）
MASK_VALUE_FIELDS = {"EmployeeID", "BirthDate", "PerfRating", "TermReason", "EduSchool"}
ORG = [f"Level{i}" for i in range(1, 8)]
FIELDS = ["EmployeeID", "EmpStatus", *ORG, "BirthDate", "Age", "AgeBand", "EduLevel", "EduSchool",
          "Seniority", "Gender", "JobGrade", "IsManager", "Function", "DL_IDL", "WorkLocation",
          "PerfRating", "TermDate", "TermType", "TermReason"]
SENS = {"BirthDate": "sensitive", "Age": "sensitive", "AgeBand": "sensitive", "TermReason": "sensitive",
        "PerfRating": "highly"}
ROLE = {  # 可看的敏感等級、是否可看個人明細
    "R1": ({"general", "sensitive", "highly"}, False),
    "R2": ({"general", "sensitive", "highly"}, False),
    "R3": ({"general", "sensitive"}, False),
    "R4": ({"general", "sensitive", "highly"}, True),
    "R5": (set(), False),
}
NUMERIC = {"Seniority", "Age"}
TERM = {"TermDate", "TermType", "TermReason"}
FieldName = Literal[tuple(FIELDS)]  # type: ignore[valid-type]


class Filter(BaseModel):
    model_config = ConfigDict(extra="forbid")
    field: FieldName
    op: Literal["eq", "ne", "in", "lt", "lte", "gt", "gte"]
    value: Union[str, float, list[str]]


class Top(BaseModel):
    model_config = ConfigDict(extra="forbid")
    by: Literal["Seniority", "Age"]
    percent: float = Field(gt=0, le=100)


class QuerySpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    analysis: Literal["aggregate", "attrition", "list"] = "aggregate"
    dataset: Literal["active", "leavers"] = "active"
    filters: list[Filter] = []
    top: Optional[Top] = None
    group_by: Optional[FieldName] = None
    metrics: list[str] = ["count"]
    fields: list[FieldName] = []
    sort_by: Optional[FieldName] = None
    limit: int = Field(default=500, ge=1, le=1000)


class Denied(Exception):
    pass


def _clean_dir() -> Path:
    return Path(os.environ.get("HRC_CLEAN_DIR", ROOT / "work"))


def _log_dir() -> Path:
    p = Path(os.environ.get("HRC_LOG_DIR", ROOT / "logs"))
    p.mkdir(parents=True, exist_ok=True)
    return p


def redact_text(s: str) -> str:
    """稽核紀錄去識別：遮蔽員工編號與日期（R5 可看稽核，但不能看到 HR 資料內容）。"""
    s = re.sub(r"[Ee]\d{6}", "E******", str(s))
    s = re.sub(r"\d{4}[-/.]\d{1,2}[-/.]\d{1,2}", "****-**-**", s)
    return re.sub(r"(?<!\d)\d{8}(?!\d)", "********", s)


def _redact_spec(raw) -> object:
    try:
        spec = json.loads(json.dumps(raw, ensure_ascii=False))
    except (TypeError, ValueError):
        return "<unserializable>"
    if isinstance(spec, dict):
        for f in spec.get("filters") or []:
            if isinstance(f, dict) and f.get("field") in MASK_VALUE_FIELDS:
                f["value"] = "***"
    return json.loads(redact_text(json.dumps(spec, ensure_ascii=False)))


def audit(event: dict) -> None:
    event = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), **event}
    with open(_log_dir() / "audit.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")


def _age(bd: pd.Series) -> pd.Series:
    d = pd.to_datetime(bd, format="%Y-%m-%d", errors="coerce")
    a = BASE_DATE.year - d.dt.year - ((d.dt.month > 2) | ((d.dt.month == 2) & (d.dt.day > 28))).astype(int)
    return a.where(d.notna())


def _band(a: float) -> str:
    if pd.isna(a):
        return ""
    lo = int(a // 5 * 5)
    return f"{lo}-{lo + 4}歲"


def _apply_review(df: pd.DataFrame, src: str, items: list[dict]) -> pd.DataFrame:
    """待確認項目只有在所有審核方都核准後才生效；未決或駁回一律 fail-closed。"""
    drop = pd.Series(False, index=df.index)
    dup_ids: set[str] = set()
    keep: dict[str, set[str]] = {}
    for p in items:
        st = review.status(p)
        if p["file"] == "both":   # 同時在兩檔：未決→兩檔都不納入；核准→只留在離職檔
            if st != "approved" or src == "active":
                drop |= df["EmployeeID"] == p["employee_id"]
        elif p["file"] != src:
            continue
        elif p["field"] == "_row":  # 同編號多筆：只保留被核准的那一筆，否則全部不納入
            dup_ids.add(p["employee_id"])
            if st == "approved":
                keep.setdefault(p["employee_id"], set()).add(p["row_hash"])
        elif st == "approved":
            df.loc[df["EmployeeID"] == p["employee_id"], p["field"]] = p["proposed"]
    if dup_ids:
        sub = df[df["EmployeeID"].isin(dup_ids)]
        hashes = sub.apply(review.row_hash, axis=1)
        for eid in dup_ids:
            k = keep.get(eid, set())
            rows = sub.index[sub["EmployeeID"] == eid]
            ok = rows[hashes[rows].isin(k)] if len(k) == 1 else rows[:0]
            drop.loc[rows.difference(ok)] = True
    return df[~drop]


@lru_cache(maxsize=4)
def _load(d_str: str, stamp: tuple) -> dict[str, pd.DataFrame]:
    d = Path(d_str)
    out = {}
    items = review.load_items(d)
    for name, fn in (("active", "active.csv"), ("leavers", "inactive.csv")):
        df = pd.read_csv(d / fn, dtype=str, keep_default_na=False)
        df = _apply_review(df, "active" if name == "active" else "inactive", items)
        for c in ("TermDate", "TermType", "TermReason"):
            if c not in df:
                df[c] = ""
        df["Seniority"] = pd.to_numeric(df["Seniority"], errors="coerce")
        df["Age"] = _age(df["BirthDate"])
        df["AgeBand"] = df["Age"].map(_band)
        out[name] = df
    return out


def load() -> tuple[dict[str, pd.DataFrame], str]:
    d = _clean_dir()
    stamp = tuple((d / f).stat().st_mtime_ns if (d / f).exists() else 0
                  for f in ("active.csv", "inactive.csv", "pending_review.json"))
    if not stamp[0]:
        raise Denied("尚未完成資料清理，請由 R5 系統管理員先執行清理。")
    version = hashlib.sha1(repr((str(d), stamp)).encode()).hexdigest()[:10]
    return _load(str(d), stamp), version


def _missing(s: pd.Series) -> pd.Series:
    return s.isna() | (s.astype(str) == "")


def _check_fields(user: User, spec: QuerySpec) -> None:
    allowed, detail = ROLE[user.role]
    if user.role == "R5":
        raise Denied("R5 系統管理員依職務分離原則不能查詢任何人資資料，只能查看稽核紀錄與管理帳號。")
    used = {f.field for f in spec.filters} | set(spec.fields)
    used |= {spec.group_by} if spec.group_by else set()
    used |= {spec.top.by} if spec.top else set()
    used |= {spec.sort_by} if spec.sort_by else set()
    used |= {m.split(":", 1)[1] for m in spec.metrics if ":" in m}
    for f in used:
        if f not in FIELDS:
            raise Denied(f"未知欄位：{f}")
        if SENS.get(f, "general") not in allowed:
            raise Denied(f"您的角色（{user.role}）不能使用「{f}」欄位（敏感等級：{SENS[f]}）。")
    if spec.analysis == "list" and not detail:
        raise Denied(f"您的角色（{user.role}）只能看統計結果，不能看個人明細。")
    if not detail and ("BirthDate" in used or "EmployeeID" in used):
        raise Denied("非人資角色只能看年齡區間，不能看出生日期或員工編號。")
    if not detail:  # 非人資角色只能看年齡區間：精確年齡不可分組／分布／排名；年齡條件只能用 5 歲倍數的 lt/gte
        if spec.group_by == "Age" or (spec.top and spec.top.by == "Age") or any(m.endswith(":Age") for m in spec.metrics):
            raise Denied("非人資角色只能看年齡區間（AgeBand），不能看精確年齡。")
        for f in spec.filters:
            if f.field == "Age" and (f.op not in ("lt", "gte") or isinstance(f.value, list) or float(f.value) % 5):
                raise Denied("非人資角色的年齡條件只能用 5 歲區間邊界（例如 未滿 35 → lt 35、35 歲以上 → gte 35）。")
        # 課／組人數少，兩次查詢相減即可反推個人 → 敏感欄位不可與 Level6/Level7 一起使用
        if any(SENS.get(f, "general") != "general" for f in used) and used & {"Level6", "Level7"}:
            raise Denied("為避免由統計結果反推個人，敏感欄位（年齡、績效、離職原因等）不能與課／組層級（Level6、Level7）"
                         "一起查詢；請改用 BU、處或部層級。")


def _scope(df: pd.DataFrame, user: User) -> pd.DataFrame:
    m = pd.Series(True, index=df.index)
    for lvl, val in user.scope:  # 完整組織路徑；任一層不符或缺值（例如待確認）都不納入
        m &= df[lvl] == val
    return df[m]


def _in_period(df: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    td = pd.to_datetime(df["TermDate"], format="%Y-%m-%d", errors="coerce")
    ok = (td >= PERIOD_START) & (td <= BASE_DATE)
    return df[ok], int((~ok).sum())


def _not_applicable(df: pd.DataFrame, field: str) -> pd.Series:
    """高階主管本來就沒有下層組織欄位：屬於「不適用」，不是缺值。"""
    if field not in ORG:
        return pd.Series(False, index=df.index)
    lower = ORG[ORG.index(field):]
    return (df["IsManager"] == "Y") & (df[lower] == "").all(axis=1)


def _small(n) -> bool:
    return isinstance(n, int) and 0 < n < K


def _mask(df: pd.DataFrame, f: Filter, full: pd.DataFrame, user: User) -> pd.Series:
    """回傳符合條件的布林遮罩；缺值一律為 False。"""
    col = df[f.field]
    vals = [str(v) for v in f.value] if isinstance(f.value, list) else [str(f.value)]
    if f.field in ORG and f.op in ("eq", "in"):  # 指定範圍外的組織 → 拒絕，而不是回傳 0
        for v in vals:
            if v not in set(df[f.field]) and v in set(full[f.field]):
                raise Denied(f"「{v}」不在您的資料權限範圍內（您的範圍：{user.scope_label}）。")
    present = ~_missing(col)
    if f.op == "in":
        return present & col.astype(str).isin(vals)
    if f.field in NUMERIC:
        v = float(vals[0])
        m = {"eq": col == v, "ne": col != v, "lt": col < v, "lte": col <= v, "gt": col > v, "gte": col >= v}[f.op]
    else:
        s, v = col.astype(str), vals[0]
        m = {"eq": s == v, "ne": s != v, "lt": s < v, "lte": s <= v, "gt": s > v, "gte": s >= v}[f.op]
    return present & m


def _dist(s: pd.Series, suppress: bool) -> dict:
    s = s[~_missing(s)]
    n = int(len(s))
    cells = {str(k): {"count": int(v), "pct": round(v / n * 100, 1)} for k, v in s.value_counts().sort_index().items()}
    if suppress:
        small = [k for k, c in cells.items() if _small(c["count"])]
        if len(small) == 1 and len(cells) > 1:  # 只隱藏一格時可由總數反推 → 再隱藏最小的另一格
            small.append(min((c["count"], k) for k, c in cells.items() if k not in small)[1])
        for k in small:
            cells[k] = {"count": f"<{K}" if _small(cells[k]["count"]) else "隱藏", "pct": None, "suppressed": True}
    return {"cells": cells, "n_valid": n}


def _metrics(df: pd.DataFrame, metrics: list[str], suppress: bool) -> dict:
    """人數 = 符合條件的全部筆數；平均與分布只用該欄位有效值，另回報有效樣本數 n_valid。"""
    n = int(len(df))
    if suppress and _small(n):
        return {"count": f"<{K}", "suppressed": True}
    out: dict = {}
    for m in metrics:
        if m == "count":
            out["count"] = n
        elif m.startswith("avg:"):
            s = df[m[4:]][~_missing(df[m[4:]])]
            nv = int(len(s))
            out[m] = ({"value": None, "n_valid": f"<{K}", "suppressed": True} if suppress and _small(nv)
                      else {"value": round(float(s.mean()), 2) if nv else None, "n_valid": nv})
        elif m.startswith("dist:"):
            out[m] = _dist(df[m[5:]], suppress)
        else:
            raise Denied(f"不支援的指標：{m}（可用 count、avg:<欄位>、dist:<欄位>）")
    return out


def run_query(user: User, raw: dict) -> dict:
    """執行查詢；永遠回傳 dict（ok=False 表示拒絕或錯誤），不丟例外。"""
    try:
        spec = QuerySpec.model_validate(raw)
        _check_fields(user, spec)
        data, version = load()
        res = _run(user, spec, data, version)
        audit({"user": user.username, "role": user.role, "event": "query", "spec": _redact_spec(raw), "status": "ok",
               "rows_used": res.get("rows_used"), "data_version": version})
        return {"ok": True, **res}
    except ValidationError as e:
        msg = f"查詢參數不合法：{e.errors()[0]['loc']} {e.errors()[0]['msg']}"
        status = "invalid"
    except Denied as e:
        msg, status = str(e), "denied"
    except Exception as e:  # 防禦：引擎錯誤也要回給模型，不能讓聊天中斷
        msg, status = f"查詢失敗：{type(e).__name__}: {e}", "error"
    audit({"user": user.username, "role": user.role, "event": "query", "spec": _redact_spec(raw), "status": status,
           "reason": redact_text(msg)})
    return {"ok": False, "status": status, "error": msg}


def _pending_in_scope(user: User) -> int:
    """尚未核准、若核准就會落在登入者範圍內的待確認筆數（確認前不納入計算）。"""
    sc = dict(user.scope)
    if not sc:
        return 0
    n = 0
    for x in review.load_items():
        if review.status(x) == "approved":
            continue
        if len(sc) == 1 and x["file"] != "both" and x["field"] != "_row":
            continue  # R1：組織欄位待確認的資料仍在全公司範圍內
        path = x.get("path") or {}
        if all(path.get(k, v) == v for k, v in sc.items() if k in ("Level2", "Level3")):
            n += 1
    return n


def _run(user: User, spec: QuerySpec, data: dict[str, pd.DataFrame], version: str = "") -> dict:
    info: dict = {"scope": user.scope_label, "basis_date": "2026-02-28", "data_version": version}
    n = _pending_in_scope(user)
    if n:
        info["pending_hrbp_review_not_included"] = n
    suppress = not ROLE[user.role][1]
    if spec.analysis == "attrition":
        return {**info, **_attrition(user, spec, data, suppress)}
    full = data[spec.dataset]
    df = _scope(full, user)
    if spec.dataset == "leavers":
        df, info["excluded_out_of_period"] = _in_period(df)
    info["dataset"] = spec.dataset
    info["rows_in_scope"] = int(len(df))
    # 篩選條件；某欄位缺值而被排除的筆數 = 符合「其他所有條件」但該欄位缺值的筆數（與條件順序無關）
    masks = [(f.field, _mask(df, f, full, user)) for f in spec.filters]
    filter_fields = {f for f, _ in masks}
    excluded: dict[str, int] = {}
    for fld in filter_fields:
        others = pd.Series(True, index=df.index)
        for f2, m in masks:
            if f2 != fld:
                others &= m
        n = int((others & _missing(df[fld])).sum())
        if n:
            excluded[fld] = n
    keep = pd.Series(True, index=df.index)
    for _, m in masks:
        keep &= m
    df = df[keep]
    if spec.top:
        miss = _missing(df[spec.top.by])
        if miss.any():
            excluded[spec.top.by] = excluded.get(spec.top.by, 0) + int(miss.sum())
        df = df[~miss]
        n = len(df)
        k = math.ceil(n * spec.top.percent / 100)
        if k:
            cutoff = df[spec.top.by].sort_values(ascending=False).iloc[k - 1]
            df = df[df[spec.top.by] >= cutoff]
            info["top"] = {"population": n, "percent": spec.top.percent, "k=ceil(n*pct)": k,
                           f"cutoff_{spec.top.by}": round(float(cutoff), 2), "selected_incl_ties": int(len(df))}
            if suppress and _small(len(df)):
                info["top"] = {"suppressed": True}
    if spec.group_by:  # 分組欄位缺值無法分組；高階主管的下層組織欄位屬「不適用」
        miss = _missing(df[spec.group_by])
        na = miss & _not_applicable(df, spec.group_by)
        if na.any():
            info["not_applicable_higher_level_managers"] = int(na.sum())
        if (miss & ~na).any():
            excluded[spec.group_by] = excluded.get(spec.group_by, 0) + int((miss & ~na).sum())
        df = df[~miss]
    info["excluded_missing_or_invalid"] = excluded
    info["rows_used"] = int(len(df))
    if spec.analysis == "list":
        cols = list(dict.fromkeys(spec.fields or ["EmployeeID", "Level3", "Level4", "Level5", "Level6"]))
        if spec.sort_by:
            df = df.sort_values(spec.sort_by, ascending=False)
        rows = df[cols].head(spec.limit)
        info["rows"] = json.loads(rows.to_json(orient="records", force_ascii=False))
        info["truncated"] = len(df) > spec.limit
        return info
    if spec.group_by:
        info["groups"] = {str(k): _metrics(g, spec.metrics, suppress) for k, g in df.groupby(spec.group_by)}
    else:
        info["result"] = _metrics(df, spec.metrics, suppress)
    return info


def _attrition(user: User, spec: QuerySpec, data: dict[str, pd.DataFrame], suppress: bool) -> dict:
    """離職率 = 當月（2026-02）離職人數 ÷ 月底在職人數，依 group_by 分組。"""
    g = spec.group_by or "Level3"
    out: dict = {"definition": "離職率 = 2026 年 2 月離職人數 ÷ 2026-02-28 在職人數", "group_by": g}
    parts, excluded, na_count = {}, {}, 0
    for name in ("active", "leavers"):
        full = data[name]
        df = _scope(full, user)
        if name == "leavers":
            df, out["excluded_out_of_period"] = _in_period(df)
        for f in spec.filters:
            if name == "active" and f.field in TERM:  # 離職欄位條件只套用在離職者（分子）
                continue
            df = df[_mask(df, f, full, user)]
        miss = _missing(df[g])
        na = miss & _not_applicable(df, g)
        na_count += int(na.sum())
        excluded[name] = int((miss & ~na).sum())
        parts[name] = df[~miss]
    act, lv = parts["active"], parts["leavers"]
    rows = []
    for k in sorted(set(act[g]) | set(lv[g])):
        a = int((act[g] == k).sum())
        l_all = lv[lv[g] == k]
        n = len(l_all)
        vol = int((l_all["TermType"] == "自願").sum())
        row = {"group": k, "leavers": n, "voluntary_leavers": vol, "active_month_end": a,
               "attrition_rate_pct": round(n / a * 100, 2) if a else None,
               "voluntary_rate_pct": round(vol / a * 100, 2) if a else None}
        if suppress:  # 小樣本：人數 1–4 的格子與由它算出的比率不顯示
            if _small(a):
                row.update(active_month_end=f"<{K}", attrition_rate_pct=None, voluntary_rate_pct=None)
            if _small(n):
                row.update(leavers=f"<{K}", attrition_rate_pct=None)
            if _small(vol):
                row.update(voluntary_leavers=f"<{K}", voluntary_rate_pct=None)
        rows.append(row)
    out["groups"] = rows
    rates = [r for r in rows if isinstance(r["attrition_rate_pct"], float)]
    counts = [r for r in rows if isinstance(r["leavers"], int)]
    out["highest_attrition_rate_groups"] = [r["group"] for r in rates if r["attrition_rate_pct"] == max(x["attrition_rate_pct"] for x in rates)] if rates else []
    out["most_leavers_groups"] = [r["group"] for r in counts if r["leavers"] == max(x["leavers"] for x in counts)] if counts else []
    if any(r["attrition_rate_pct"] > 100 for r in rates):
        out["note"] = "部分單位離職率超過 100%：分母為月底在職人數，因此可能超過 100%。"
    out["excluded_missing_group_field"] = excluded
    if na_count:
        out["not_applicable_higher_level_managers"] = na_count
    out["scope"] = user.scope_label
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--spec", required=True, help="QuerySpec JSON")
    a = ap.parse_args()
    import getpass

    from auth import login
    name = os.environ.get("HRC_USER_ID", "")
    user = login(name, os.environ.get("HRC_PASSWORD") or getpass.getpass("密碼："))
    if user is None:
        audit({"user": name if name else "?", "event": "cli_login", "status": "failed"})
        print(json.dumps({"ok": False, "error": "身分驗證失敗（需 HRC_USER_ID 與密碼）"}, ensure_ascii=False))
        return 1
    print(json.dumps(run_query(user, json.loads(a.spec)), ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
