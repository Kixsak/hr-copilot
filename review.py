"""待人工確認項目：建立、狀態判定、R4 審核（伺服器端權責檢查）與歷史紀錄。

決策（使用者確認）：需要的每一方 HRBP 都核准才生效；任一方駁回即駁回；駁回後欄位維持缺值（fail-closed）。
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

ROOT = Path(__file__).parent


def _path(d: Path | None = None) -> Path:
    return (d or Path(os.environ.get("HRC_CLEAN_DIR", ROOT / "work"))) / "pending_review.json"


def row_hash(row) -> str:
    return hashlib.sha1("|".join(str(v) for v in row.values).encode("utf-8")).hexdigest()[:12]


def new_item(src, row, field, issue, current, proposed, reviewers, path, row_hash="") -> dict:
    h = hashlib.sha1(f"{current}|{proposed}|{row_hash}".encode("utf-8")).hexdigest()[:8]
    eid = row["EmployeeID"]
    return {"id": f"{src}:{eid}:{field}:{h}", "file": src, "employee_id": eid, "bu": row.get("Level3", ""),
            "field": field, "issue": issue, "current": current, "proposed": proposed, "reviewers": reviewers,
            "path": path, "row_hash": row_hash, "approvals": {}, "history": [], "status": "pending"}


def status(item: dict) -> str:
    reviewers = item.get("reviewers") or []
    vals = [item.get("approvals", {}).get(str(i), {}).get("decision") for i in range(len(reviewers))]
    if "rejected" in vals:
        return "rejected"
    if vals and all(v == "approved" for v in vals):
        return "approved"
    return "pending"


def _matches(user, req: dict) -> bool:
    mine = {"Level2": user.assigned_bg, "Level3": user.assigned_bu}
    return user.role == "R4" and user.active and all(mine.get(k) == v for k, v in req.items())


def load_items(d: Path | None = None) -> list[dict]:
    p = _path(d)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else []


def get(item_id: str) -> dict | None:
    return next((x for x in load_items() if x["id"] == item_id), None)


def items_for(user) -> list[dict]:
    return [x for x in load_items() if any(_matches(user, r) for r in x.get("reviewers") or [])]


def decide(user, item_id: str, decision: str) -> dict:
    if decision not in ("approved", "rejected"):
        raise ValueError("decision 只能是 approved 或 rejected")
    items = load_items()
    x = next((i for i in items if i["id"] == item_id), None)
    if x is None:
        raise KeyError(item_id)
    sides = [i for i, r in enumerate(x.get("reviewers") or []) if _matches(user, r)]
    if not sides:
        raise PermissionError("您不是此項目的審核者")
    approvals = x.setdefault("approvals", {})
    if any(v.get("by") == user.username and int(k) not in sides for k, v in approvals.items()):
        raise PermissionError("同一人不能代表兩方審核")
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    for i in sides:
        approvals[str(i)] = {"decision": decision, "by": user.username, "at": now}
        x.setdefault("history", []).append({"side": i, "decision": decision, "by": user.username, "at": now})
    x["status"] = status(x)
    p = _path()
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, p)
    return x
