"""复采数据核验与闭锁解除判定。

同一批复测重试不新增闭锁记录、不重复计时：闭锁台账按“炉批 + 边界起点”唯一，
复采通过只把既有记录翻成已解除。解除失败（覆盖不完整或仍不达标）时保留旧版
结论，台账与计时均不变。
"""

from copy import deepcopy

from app.errors import BusinessError
from app.manifest import digest
from app.modules.m8_lockout.boundary import CALIBRATION_FAULTS
from app.modules.m8_lockout.service import build_lockout
from app.pipeline import calculate
from app.quality import instant, rows, token

REQUIRED_READING_KEYS = {"zone", "time", "unit", "value"}


def validate_resample(data, plan, boundaries):
    """复采报文必须逐热区、逐边界完整覆盖，且读数本身可被采信。

    复采只能补闭锁边界内的证据：落在全部边界之外的读数视为越权重采，拒绝，
    防止复采改写可信时段的原始结论。
    """
    records = list(rows(data, "readings"))
    points = []
    for row, raw in records:
        try:
            zone = token(raw["zone"])
            time = instant(raw["time"])
            if zone not in plan["zones"] or not REQUIRED_READING_KEYS <= set(raw):
                raise ValueError
        except (KeyError, TypeError, ValueError) as error:
            raise BusinessError(
                "BAD_RESAMPLE", f"复采第 {row} 行无法采信"
            ) from error
        if not any(
            zone == item["zone"] and item["start"] <= time < item["end"]
            for item in boundaries
        ):
            raise BusinessError(
                "BAD_RESAMPLE",
                f"复采第 {row} 行落在闭锁边界之外，不得改写可信时段",
            )
        points.append({"row": row, "zone": zone, "time": time})
    if not points:
        raise BusinessError("BAD_RESAMPLE", "复采读数不能为空")
    if "calibrations" in data and not isinstance(data["calibrations"], list):
        raise BusinessError("BAD_RESAMPLE", "复采校准须为列表")
    coverage = []
    for boundary in boundaries:
        inside = [
            point
            for point in points
            if point["zone"] == boundary["zone"]
            and boundary["start"] <= point["time"] < boundary["end"]
        ]
        coverage.append(
            {
                "zone": boundary["zone"],
                "start": boundary["start"],
                "end": boundary["end"],
                "covered_rows": sorted(point["row"] for point in inside),
                "complete": bool(inside),
            }
        )
    if any(not item["complete"] for item in coverage):
        missing = [
            f"{item['zone']} {item['start']}-{item['end']}"
            for item in coverage
            if not item["complete"]
        ]
        raise BusinessError(
            "RESAMPLE_INCOMPLETE", "复采未覆盖全部闭锁边界：" + "、".join(missing)
        )
    return coverage


def merge_resample(original, data, boundaries=None):
    """把复采读数与替换校准并入来源快照。

    落在闭锁边界内的原读数已被判定为不可信，复采读数按“同点复测”直接替换
    原行（原行号与原值仍保留在来源快照之外的复采覆盖清单里可追溯），避免按
    普通冲突把新旧两行双双隔离。边界之外的原始读数原样保留，复采不得改写。
    复采校准若提交，则整体替换该来源的校准登记（探头重校后的新台账）。
    """
    merged = deepcopy(original)
    if boundaries:
        replaced = []

        def in_boundary(raw):
            try:
                time = instant(raw["time"])
            except (KeyError, TypeError, ValueError):
                return False
            return any(
                raw.get("zone") == item["zone"]
                and item["start"] <= time < item["end"]
                for item in boundaries
            )

        kept = []
        for index, raw in enumerate(merged.get("readings", []), 1):
            if in_boundary(raw):
                replaced.append(index)
            else:
                kept.append(raw)
        merged["readings"] = kept + list(data.get("readings", []))
        merged["resample_replaced_rows"] = replaced
    else:
        merged["readings"] = list(original.get("readings", [])) + list(
            data.get("readings", [])
        )
    if isinstance(data.get("calibrations"), list):
        merged["calibrations"] = data["calibrations"]
    return merged


def _faults_gone(result, boundaries):
    zones = {item["zone"] for item in boundaries}
    for marker in result["calibration"]["isolated"]:
        if (
            marker.get("code") in CALIBRATION_FAULTS
            and isinstance(marker.get("raw"), dict)
            and marker["raw"].get("zone") in zones
        ):
            return False
    return True


def evaluate_resample(original, resample):
    """返回复采复算结论；不触碰台账，由 service 决定是否落账。

    解除判定只回答“本道闭锁边界是否已补齐可信证据”：原炉批中与漂移无关的
    既有断档（缺采、配方待核等）不由复采闭锁负责，仍留在正式放行闸口处理。
    """
    base = calculate(original)
    lockout = build_lockout(base, original)
    boundaries = lockout["boundaries"]
    if not boundaries:
        raise BusinessError("LOCKOUT_NOT_FOUND", "该炉批没有待解除的复采闭锁", 404)
    plan = base["intake"]["plan"]
    coverage = validate_resample(resample, plan, boundaries)
    merged = merge_resample(original, resample, boundaries)
    result = calculate(merged)
    reopened = build_lockout(result, merged)
    residual = [
        item
        for item in reopened["boundaries"]
        if any(
            item["zone"] == old["zone"]
            and item["start"] == old["start"]
            and item["end"] == old["end"]
            for old in boundaries
        )
    ]
    calibration_clean = _faults_gone(result, boundaries)
    # 边界时段在复采后不得再出现任何缺证（含漂移、失采或冲突）。
    boundary_unproven = _boundary_unproven(result, boundaries)
    passed = not residual and calibration_clean and not boundary_unproven
    return {
        "passed": passed,
        "coverage": coverage,
        "resample_sha256": digest(resample),
        "result": result,
        "lockout": reopened,
        "failures": []
        if passed
        else _failure_reasons(
            residual, calibration_clean, boundary_unproven, result["reconcile"]
        ),
    }


def _boundary_unproven(result, boundaries):
    hit = []
    for section in result["soak"]["sections"]:
        if section["state"] != "unproven":
            continue
        locked_zones = {
            item["zone"]
            for item in boundaries
            if section["start"] < item["end"] and section["end"] > item["start"]
        }
        if locked_zones and any(
            zone["zone"] in locked_zones and zone["value_mc"] is None
            for zone in section["zones"]
        ):
            hit.append(section)
    return hit


def _failure_reasons(residual, calibration_clean, boundary_unproven, balance):
    reasons = []
    if residual:
        reasons.append("闭锁边界内仍存在不可信校准")
    if not calibration_clean:
        reasons.append("校准失效记录未消除")
    if boundary_unproven:
        reasons.append("闭锁边界复采后仍缺证")
    if balance["status"] != "MATCHED":
        reasons.append("炉批账未对平")
    return reasons
