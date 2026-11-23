"""复采闭锁台账服务（M8）。

台账记录按“炉批 + 热区 + 边界起止”唯一：
* 首次预览异常即把边界登记为 LOCKED（提交复采时补登亦可，结论一致）；
* 同批复测重试命中同一 id，不新增记录、不重复计时；
* 复采通过才翻成 RELEASED，并固化解除后的复算结果；
* 解除失败保留旧版结论，台账内容与已计时长原样不动。
"""

from app.catalog import source_data
from app.errors import BusinessError
from app.manifest import digest
from app.modules.m8_lockout.resample import evaluate_resample
from app.modules.m8_lockout.service import build_lockout
from app.pipeline import calculate

LOCKED = "LOCKED"
RELEASED = "RELEASED"


def _record_id(source, plan, boundary):
    return "lock-" + digest(
        {
            "source": source,
            "batch": plan["batch"],
            "zone": boundary["zone"],
            "start": boundary["start"],
            "end": boundary["end"],
        }
    )[:16]


def inspect(ledger, source):
    data = source_data(ledger, source)
    result = calculate(data)
    lockout = build_lockout(result, data)
    plan = result["intake"]["plan"]
    records = []
    for boundary in lockout["boundaries"]:
        record = ledger.lockout_optional(
            _record_id(source, plan, boundary)
        )
        records.append(record or _open_record(source, plan, boundary))
    lockout["records"] = records
    lockout["all_released"] = bool(records) and all(
        item["status"] == RELEASED for item in records
    )
    lockout["release_view"] = (
        records[0]["release_result"] if lockout["all_released"] else None
    )
    return lockout


def _open_record(source, plan, boundary):
    return {
        "id": _record_id(source, plan, boundary),
        "batch": plan["batch"],
        "source": source,
        "status": LOCKED,
        "zone": boundary["zone"],
        "start": boundary["start"],
        "end": boundary["end"],
        "seconds": boundary["seconds"],
        "attempts": 0,
        "resample_sha256": None,
        "release_result": None,
        "failure_reasons": [],
    }


def submit_resample(ledger, source, payload, operator):
    data = source_data(ledger, source)
    result = calculate(data)
    plan = result["intake"]["plan"]
    lockout = build_lockout(result, data)
    if not lockout["boundaries"]:
        raise BusinessError("LOCKOUT_NOT_FOUND", "该炉批没有待解除的复采闭锁", 404)
    saved = []
    for boundary in lockout["boundaries"]:
        identifier = _record_id(source, plan, boundary)
        record = ledger.lockout_optional(identifier) or _open_record(
            source, plan, boundary
        )
        ledger.put_lockout(record)
        saved.append(ledger.lockout(identifier))
    verdict = evaluate_resample(data, payload)
    updated = []
    for record in saved:
        current = ledger.lockout(record["id"])
        if current["status"] == RELEASED:
            updated.append(current)
            continue
        current["attempts"] += 1
        if verdict["passed"]:
            current["status"] = RELEASED
            current["resample_sha256"] = verdict["resample_sha256"]
            current["resample_input"] = payload
            current["operator"] = operator
            current["release_result"] = verdict["result"]
            current["failure_reasons"] = []
        else:
            # 解除失败：保留旧版结论，只登记尝试次数，边界与计时不变。
            current["failure_reasons"] = verdict["failures"]
        ledger.update_lockout(current["id"], current)
        updated.append(ledger.lockout(current["id"]))
    return {
        "passed": verdict["passed"],
        "failures": verdict["failures"],
        "coverage": verdict["coverage"],
        "records": updated,
        "release_result": verdict["result"] if verdict["passed"] else None,
    }
