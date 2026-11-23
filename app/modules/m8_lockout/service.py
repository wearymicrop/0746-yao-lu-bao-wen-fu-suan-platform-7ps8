"""复采闭锁对既有复算结果的统一覆盖。

边界一经划定，校准（证据标记）、热区对齐（有效读数带）、保温计时（达标 /
缺证分段）与炉批勾稽全部使用同一边界，避免各模块各判一段。覆盖不修改原始
复算结果，而是在 M8 输出中给出待补证视图。
"""

from copy import deepcopy

from app.modules.m4_soak.service import measure
from app.modules.m5_gaps.service import revisit
from app.modules.m6_reconcile.service import reconcile
from app.modules.m8_lockout.boundary import contains, derive_boundaries

LOCK_CODE = "LOCKOUT_PENDING"


def apply_alignment(alignment, boundaries):
    overlay = deepcopy(alignment)
    edges = set(overlay["breakpoints"])
    for zone, spans in overlay["zones"].items():
        clipped = []
        for span in spans:
            if not contains(boundaries, zone, span["start"], span["end"]):
                clipped.append(span)
                continue
            # 闭锁边界把读数带切断：边界外的可信片段保留，边界内一律摘除，
            # 防止上一次合格读数跨越漂移区间继续沿用。
            cuts = [span["start"], span["end"]]
            for item in boundaries:
                if item["zone"] != zone:
                    continue
                if span["start"] < item["start"] < span["end"]:
                    cuts.append(item["start"])
                if span["start"] < item["end"] < span["end"]:
                    cuts.append(item["end"])
            cuts = sorted(set(cuts))
            for start, end in zip(cuts, cuts[1:]):
                if contains(boundaries, zone, start, end):
                    continue
                clipped.append(dict(span, start=start, end=end))
                edges.update((start, end))
            edges.update(cuts)
        overlay["zones"][zone] = sorted(clipped, key=lambda item: item["start"])
    for item in boundaries:
        edges.update((item["start"], item["end"]))
    overlay["breakpoints"] = sorted(edges)
    return overlay


def apply_soak(data, plan, alignment_overlay, boundaries):
    """复用 M4 计时引擎按闭锁后的对齐带重算，再覆盖边界时段状态。"""
    soak = measure(data, plan, alignment_overlay)
    for section in soak["sections"]:
        locked_zones = {
            item["zone"]
            for item in boundaries
            if section["start"] < item["end"] and section["end"] > item["start"]
        }
        if not locked_zones:
            continue
        codes = set(section["codes"]) - {"PROVEN"}
        codes.add(LOCK_CODE)
        for zone_row in section["zones"]:
            if zone_row["zone"] not in locked_zones:
                continue
            zone_row["value_mc"] = None
            zone_row["reading_rows"] = []
            zone_row["calibration_row"] = None
            zone_row["code"] = LOCK_CODE
        section["codes"] = sorted(codes)
        section["state"] = "unproven"
    # 缺证分段切断连续达标，重新汇总 streak 与最长连续。
    streaks = []
    for section in soak["sections"]:
        if section["state"] != "proven":
            continue
        if (
            streaks
            and streaks[-1]["end"] == section["start"]
            and streaks[-1]["version"] == section["version"]
        ):
            streaks[-1]["end"] = section["end"]
            streaks[-1]["seconds"] += section["seconds"]
        else:
            streaks.append(
                {
                    "start": section["start"],
                    "end": section["end"],
                    "seconds": section["seconds"],
                    "version": section["version"],
                }
            )
    soak["streaks"] = streaks
    soak["longest_s"] = max((item["seconds"] for item in streaks), default=0)
    soak["goal_met"] = soak["longest_s"] >= plan["goal_s"]
    return soak


def build_lockout(result, data):
    plan = result["intake"]["plan"]
    boundaries = derive_boundaries(result["calibration"], plan)
    alignment = apply_alignment(result["alignment"], boundaries)
    soak = apply_soak(data, plan, alignment, boundaries)
    gaps = revisit(soak)
    balance = reconcile(result["intake"], soak, gaps)
    return {
        "boundaries": boundaries,
        "boundary_count": len(boundaries),
        "locked_s": sum(item["seconds"] for item in boundaries),
        "zones": sorted({item["zone"] for item in boundaries}),
        "batches": [plan["batch"]] if boundaries else [],
        "alignment": alignment,
        "soak": soak,
        "gaps": gaps,
        "reconcile": balance,
        "released": False,
    }
