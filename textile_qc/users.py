"""演示用户与角色。

角色权限：
- inspector 初检工：录入检验/疵点、上传照片、查看照片
- reinspector 复检工：接收/完成复检任务、查看照片（不能上传/修改初检照片）
- manager 主任：放行（含长度不足特许）、退修调度、查看照片
"""
from __future__ import annotations

USERS = {
    "wang": {"name": "王检", "roles": ["inspector"]},
    "zhao": {"name": "赵验", "roles": ["inspector"]},
    "li":   {"name": "李复", "roles": ["reinspector"]},
    "chen": {"name": "陈核", "roles": ["reinspector"]},
    "sun":  {"name": "孙兼", "roles": ["inspector", "reinspector"]},  # 兼岗：可初检也可复检
    "zhang": {"name": "张主任", "roles": ["manager"]},
}

PHOTO_VIEW_ROLES = {"inspector", "reinspector", "manager"}
PHOTO_UPLOAD_ROLES = {"inspector", "manager"}
CONCESSION_ROLES = {"manager"}


def get_user(user_id: str) -> dict:
    if user_id not in USERS:
        raise LookupError(f"用户不存在: {user_id}")
    return USERS[user_id]


def has_role(user_id: str, role: str) -> bool:
    u = USERS.get(user_id)
    return bool(u and role in u["roles"])
