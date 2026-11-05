"""帳號、密碼驗證、帳號生命週期與資料範圍。

- users.json 只存 PBKDF2 雜湊。
- R1–R3 的資料範圍 = 登入者本人員工資料的完整組織路徑（Level1…最深一層），不寫死；R4 = 指派的 BG/BU。
- 職責分離：R5 只能建立「未啟用」帳號且不可覆寫既有帳號；R1–R4 帳號需由負責的 HRBP 啟用，R5 帳號需另一位 R5 啟用。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).parent
ORG = [f"Level{i}" for i in range(1, 8)]
ROLE_NAMES = {"R1": "集團高階主管", "R2": "BG 主管", "R3": "BU 主管", "R4": "HRBP", "R5": "系統管理員"}
LOCK_N, LOCK_SEC = 5, 900
_FAILS: dict[str, list[float]] = {}
_DUMMY = ("00" * 16, "0" * 64)


def _users_path() -> Path:
    return Path(os.environ.get("HRC_USERS_FILE", ROOT / "users.json"))


def _active_csv() -> Path:
    return Path(os.environ.get("HRC_CLEAN_DIR", ROOT / "work")) / "active.csv"


@dataclass(frozen=True)
class User:
    username: str
    role: str
    employee_id: str = ""
    assigned_bg: str = ""
    assigned_bu: str = ""
    active: bool = False

    @property
    def scope(self) -> tuple:
        if self.role == "R4":
            return (("Level2", self.assigned_bg), ("Level3", self.assigned_bu))
        if self.role == "R5":
            return ()
        path, mgr = own_path(self.employee_id)
        if not _role_level_ok(self.role, path, mgr):
            raise PermissionError("帳號角色與本人組織層級不符，無法決定資料範圍")
        return path

    @property
    def scope_label(self) -> str:
        if self.role == "R5":
            return "無（僅稽核紀錄）"
        sc = self.scope
        return " / ".join(v for _, v in sc) + ("（全公司）" if len(sc) == 1 else "（含以下單位）")


def own_path(employee_id: str) -> tuple[tuple, str]:
    p = _active_csv()
    return _own_path(employee_id, str(p), p.stat().st_mtime_ns if p.exists() else 0)


@lru_cache(maxsize=256)
def _own_path(employee_id: str, p: str, mtime: int) -> tuple[tuple, str]:
    df = pd.read_csv(p, dtype=str, keep_default_na=False, usecols=["EmployeeID", "IsManager", *ORG])
    row = df[df["EmployeeID"] == employee_id]
    if row.empty:
        raise PermissionError("找不到登入者的員工資料，無法決定資料範圍")
    r = row.iloc[0]
    filled = [c for c in ORG if r[c]]
    if not filled:
        raise PermissionError("登入者沒有組織資料，無法決定資料範圍")
    path = tuple((c, r[c]) for c in ORG[:ORG.index(filled[-1]) + 1])
    if any(v == "" for _, v in path):  # 中間層缺值（例如 Level2 待 HRBP 確認）→ fail-closed
        raise PermissionError("登入者的組織路徑不完整（可能待 HRBP 確認），無法決定資料範圍")
    return path, r["IsManager"]


def _role_level_ok(role: str, path: tuple, is_manager: str) -> bool:
    depth = len(path)
    return is_manager == "Y" and {"R1": depth == 1, "R2": depth == 2, "R3": depth >= 3}.get(role, False)


def _hash(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 200_000).hex()


def _read() -> dict:
    p = _users_path()
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def _write(data: dict) -> None:
    p = _users_path()
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, p)


def get_user(username: str) -> User | None:
    u = _read().get(username)
    if not u:
        return None
    return User(username, u["role"], u.get("employee_id", ""), u.get("assigned_bg", ""), u.get("assigned_bu", ""),
                bool(u.get("active", False)))


def login_status(username: str, password: str) -> tuple[User | None, str]:
    """回傳 (使用者, 原因)；原因：ok / failed / locked / inactive。畫面上一律顯示同一句錯誤訊息。"""
    now = time.time()
    fails = [t for t in _FAILS.get(username, []) if now - t < LOCK_SEC]
    _FAILS[username] = fails
    if len(fails) >= LOCK_N:
        return None, "locked"
    u = _read().get(username)
    salt, expected = (u["salt"], u["hash"]) if u else _DUMMY  # 帳號不存在也做一次雜湊，避免時間差
    if not hmac.compare_digest(_hash(password, salt), expected) or not u:
        fails.append(now)
        return None, "failed"
    if not u.get("active", False):
        return None, "inactive"
    _FAILS.pop(username, None)
    return get_user(username), "ok"


def login(username: str, password: str) -> User | None:
    return login_status(username, password)[0]


def reset_lockouts() -> None:
    _FAILS.clear()


def _assignment_exists(bg: str, bu: str) -> bool:
    df = pd.read_csv(_active_csv(), dtype=str, keep_default_na=False, usecols=["Level2", "Level3"])
    return bool(bg and bu and ((df["Level2"] == bg) & (df["Level3"] == bu)).any())


def create_account(creator: User | None, username: str, password: str, role: str, employee_id: str = "",
                   assigned_bg: str = "", assigned_bu: str = "") -> None:
    """R5 建立帳號：一律未啟用、不可覆寫既有帳號、角色需與組織層級相符。"""
    if creator is None or creator.role != "R5" or not creator.active:
        raise PermissionError("只有 R5 系統管理員可以建立帳號")
    if role not in ROLE_NAMES:
        raise ValueError("角色不存在")
    data = _read()
    if not username or username in data:
        raise ValueError("帳號已存在或為空（不可覆寫既有帳號）")
    if len(password) < 8:
        raise ValueError("密碼至少 8 碼")
    if role in ("R1", "R2", "R3"):
        try:
            path, mgr = own_path(employee_id)
        except PermissionError as e:
            raise ValueError(str(e)) from e
        if not _role_level_ok(role, path, mgr):
            raise ValueError("角色與員工的組織層級不符（R1=集團、R2=BG、R3=BU 以下的主管）")
        employee_id, assigned_bg, assigned_bu = employee_id, "", ""
    elif role == "R4":
        if not _assignment_exists(assigned_bg, assigned_bu):
            raise ValueError("指派的 BG／BU 組合不存在")
        employee_id = ""
    salt = secrets.token_hex(16)
    data[username] = {"role": role, "employee_id": employee_id, "assigned_bg": assigned_bg,
                      "assigned_bu": assigned_bu, "salt": salt, "hash": _hash(password, salt), "active": False,
                      "created_by": creator.username, "created_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    _write(data)


def _can_activate(activator: User, target: dict, username: str) -> bool:
    if not activator.active or target.get("created_by") == activator.username or activator.username == username:
        return False
    if target["role"] == "R5":
        return activator.role == "R5"
    if activator.role != "R4":
        return False
    if target["role"] == "R4":
        return target.get("assigned_bg") == activator.assigned_bg
    path = dict(own_path(target["employee_id"])[0])
    if target["role"] == "R1":
        return True
    if target["role"] == "R2":
        return path.get("Level2") == activator.assigned_bg
    return path.get("Level2") == activator.assigned_bg and path.get("Level3") == activator.assigned_bu


def activate(activator: User | None, username: str) -> None:
    data = _read()
    t = data.get(username)
    if not t or t.get("active"):
        raise ValueError("帳號不存在或已啟用")
    if activator is None or not _can_activate(activator, t, username):
        raise PermissionError("您無權啟用此帳號")
    t.update(active=True, activated_by=activator.username, activated_at=time.strftime("%Y-%m-%dT%H:%M:%S"))
    _write(data)


def pending_accounts_for(user: User) -> list[str]:
    out = []
    for name, t in _read().items():
        if not t.get("active"):
            try:
                if _can_activate(user, t, name):
                    out.append(name)
            except PermissionError:
                continue
    return out


def list_accounts() -> list[dict]:
    return [{"username": k, "role": v["role"], "active": v.get("active", False), "created_by": v.get("created_by", "")}
            for k, v in _read().items()]


def bootstrap_user(username: str, password: str, role: str, employee_id: str = "", assigned_bg: str = "",
                   assigned_bu: str = "") -> None:
    """系統初始化用（需要主機檔案權限，不經過 UI）：建立已啟用帳號。"""
    data = _read()
    salt = secrets.token_hex(16)
    data[username] = {"role": role, "employee_id": employee_id, "assigned_bg": assigned_bg, "assigned_bu": assigned_bu,
                      "salt": salt, "hash": _hash(password, salt), "active": True, "created_by": "bootstrap"}
    _write(data)
