from app.errors import BusinessError
from app.modules.m8_lockout.service import build_lockout


def approval(result, data):
    balance = result["reconcile"]
    if balance["status"] != "MATCHED":
        raise BusinessError("UNBALANCED", "炉批尚未对平", 409)
    lockout = build_lockout(result, data)
    if lockout["boundaries"]:
        raise BusinessError(
            "LOCKOUT_PENDING",
            "存在复采闭锁待补证时段，复采通过前不得正式放行",
            409,
        )
    accepted = balance["goal_met"] and balance["unproven_s"] == 0
    return {
        "status": "RELEASED" if accepted else "REVIEW",
        "batch": result["intake"]["plan"]["batch"],
        "decision": "轨道证据齐全" if accepted else "转工艺复核",
    }
