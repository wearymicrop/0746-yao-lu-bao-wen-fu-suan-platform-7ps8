"""复采闭锁边界划定（M8）。

只罚掉报错读数会让相邻保温数据继续混入不可靠温度，整段回退又把复采能补上的
时长作废。本模块按核验间隔划定异常影响边界：

* 起点：失效校准窗口（偏移超限 / 校准重叠 / 校准缺失）在计划窗内的最早起点；
  该点之前的最后一次合格读数仍可信，不纳入闭锁。
* 终点：失效窗口结束后，该热区重新取得合格校准与合格读数的时刻；没有恢复点
  时顺延到计划结束。
* 同一热区相邻或首尾相接的失效窗口并入同一道边界，跨热区分开建账。

校准、热区对齐与保温计时共用这道边界，未复采通过前该时段只能作待补证。
"""

from bisect import bisect_left

from app.quality import utc

# 校准侧导致探头不可信的原因码，均参与边界划定。
CALIBRATION_FAULTS = frozenset(
    {"DRIFT_LIMIT", "CALIBRATION_GAP", "CALIBRATION_OVERLAP"}
)


def fault_windows(calibration, plan):
    """从校准登记中还原各热区的失效时间窗（偏移超限或登记缺口）。"""
    windows = []
    for zone in plan["zones"]:
        schedule = sorted(
            calibration["register"].get(zone, []),
            key=lambda item: item["start"],
        )
        covered = []
        for item in schedule:
            start, end = max(item["start"], plan["start"]), min(
                item["end"], plan["end"]
            )
            if start >= end:
                continue
            if abs(item["offset_mc"]) > plan["max_drift_mc"]:
                windows.append({"zone": zone, "start": start, "end": end})
            covered.append((start, end))
        covered.sort()
        cursor = plan["start"]
        for start, end in covered:
            if start > cursor:
                windows.append({"zone": zone, "start": cursor, "end": start})
            cursor = max(cursor, end)
        if cursor < plan["end"]:
            windows.append({"zone": zone, "start": cursor, "end": plan["end"]})
    # 登记被整体拒绝（重叠 / 坏行）时，隔离记录兜底为整窗待证。
    rejected = {
        marker["raw"]["zone"]
        for marker in calibration["isolated"]
        if marker.get("code") in CALIBRATION_FAULTS
        and isinstance(marker.get("raw"), dict)
        and marker["raw"].get("zone") in plan["zones"]
    }
    for zone in rejected:
        windows.append(
            {"zone": zone, "start": plan["start"], "end": plan["end"], "fallback": True}
        )
    return windows


def trusted_points(calibration):
    """各热区取得合格校准的读数时刻（升序）。"""
    points = {}
    for point in calibration["corrected"]:
        points.setdefault(point["zone"], set()).add(point["time"])
    return {zone: sorted(times) for zone, times in points.items()}


def merge_zone_windows(windows, plan, trusted):
    """把同热区相接的失效窗口合并为闭锁边界并求恢复点。"""
    boundaries = []
    for zone in plan["zones"]:
        zone_windows = [
            dict(item)
            for item in windows
            if item["zone"] == zone and not item.get("fallback")
        ]
        if any(
            item["zone"] == zone and item.get("fallback") for item in windows
        ) and not zone_windows:
            zone_windows = [
                {"zone": zone, "start": plan["start"], "end": plan["end"]}
            ]
        zone_windows.sort(key=lambda item: (item["start"], item["end"]))
        merged = []
        for window in zone_windows:
            if merged and window["start"] <= merged[-1]["end"]:
                merged[-1]["end"] = max(merged[-1]["end"], window["end"])
            else:
                merged.append(dict(window))
        times = trusted.get(zone, [])
        for boundary in merged:
            index = bisect_left(times, boundary["end"])
            recovery = times[index] if index < len(times) else None
            boundary["recovery"] = recovery
            boundary["end"] = (
                min(recovery, plan["end"]) if recovery is not None else plan["end"]
            )
            boundary["seconds"] = boundary["end"] - boundary["start"]
            boundary["time"] = utc(boundary["start"])
            boundary["recovery_time"] = (
                utc(boundary["recovery"]) if recovery is not None else None
            )
            boundaries.append(boundary)
    return boundaries


def derive_boundaries(calibration, plan):
    return merge_zone_windows(
        fault_windows(calibration, plan), plan, trusted_points(calibration)
    )


def contains(boundaries, zone, start, end):
    """判断某热区的时段 [start, end) 是否落入任一闭锁边界。"""
    return any(
        item["zone"] == zone and start < item["end"] and end > item["start"]
        for item in boundaries
    )
