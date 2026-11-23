from app.api import snapshot
from app.errors import BusinessError
from app.manifest import canonical, digest, manifest
from app.modules.m7_archive.signoff import approval
from app.modules.m8_lockout.resample import merge_resample
from app.quality import token
from app.catalog import source_data


def prepare(ledger, source, operator, released_lockout=None):
    if not source:
        raise BusinessError("CHOOSE_SOURCE", "请选择数据来源")
    try:
        operator = token(operator)
        if len(operator) > 32:
            raise ValueError
    except ValueError as error:
        raise BusinessError("OPERATOR_REQUIRED", "请输入 1 至 32 字的签核人") from error
    if released_lockout is not None:
        # 复采解除后的放行：凭证固化复采合并输入与解除后复算结果，
        # 并附带闭锁解除记录作为追溯证据。
        data = released_lockout["merged_input"]
        result = released_lockout["release_result"]
        check = approval(result, data)
        proof = manifest(source, data, operator, result, check)
        proof["lockout_records"] = [
            item["id"] for item in released_lockout["records"]
        ]
        proof["digest"] = digest(
            {key: value for key, value in proof.items() if key != "digest"}
        )
        return {
            "id": "run-" + proof["digest"],
            "manifest": proof,
            "approval": check,
            "result": result,
            "input": data,
            "lockout_records": released_lockout["records"],
        }
    data = source_data(ledger, source)
    result = snapshot(canonical(data))
    check = approval(result, data)
    proof = manifest(source, data, operator, result, check)
    return {
        "id": "run-" + proof["digest"],
        "manifest": proof,
        "approval": check,
        "result": result,
        "input": data,
    }


def released_lockout_view(ledger, source):
    from app.modules.m8_lockout.ledger_service import inspect

    lockout = inspect(ledger, source)
    if not lockout["boundaries"] or not lockout["all_released"]:
        raise BusinessError(
            "LOCKOUT_PENDING", "复采闭锁尚未全部解除，不能按复采结论放行", 409
        )
    records = lockout["records"]
    payload = records[0].get("resample_input")
    if payload is None:
        raise BusinessError(
            "LOCKOUT_EVIDENCE_MISSING", "复采输入证据缺失，无法归档", 409
        )
    boundaries = [
        {"zone": item["zone"], "start": item["start"], "end": item["end"]}
        for item in records
    ]
    merged_input = merge_resample(source_data(ledger, source), payload, boundaries)
    return {
        "records": records,
        "release_result": records[0]["release_result"],
        "merged_input": merged_input,
    }


def archive(ledger, source, operator, use_lockout=False):
    released = released_lockout_view(ledger, source) if use_lockout else None
    record = prepare(ledger, source, operator, released)
    return dict(record, reused=ledger.put_run(record))


def rerun(ledger, run_id):
    record = ledger.run(run_id)
    manifest_data = record["manifest"]
    use_lockout = bool(manifest_data.get("lockout_records"))
    released = (
        released_lockout_view(ledger, manifest_data["source"])
        if use_lockout
        else None
    )
    fresh = prepare(
        ledger, manifest_data["source"], manifest_data["operator"], released
    )
    if fresh != record:
        raise BusinessError(
            "MANIFEST_CHANGED", "来源或规则版本发生变化，请另行归档", 409
        )
    return dict(record, reused=True)
