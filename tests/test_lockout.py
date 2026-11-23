import copy

import pytest

from app.errors import BusinessError
from app.modules.m7_archive.bundle import consistent
from app.modules.m7_archive.signoff import approval
from app.modules.m8_lockout.boundary import derive_boundaries
from app.modules.m8_lockout.resample import evaluate_resample
from app.modules.m8_lockout.service import LOCK_CODE, build_lockout
from app.pipeline import calculate


def corrected_calibrations(dirty):
    calibrations = copy.deepcopy(dirty["calibrations"])
    # 漂移窗口（第 4 行，09:34–09:36，4700mc）重校回限值内。
    calibrations[3]["offset_mc"] = 1100
    return calibrations


def resample_payload(dirty):
    return {
        "readings": [
            {
                "zone": "南墙",
                "time": "2026-11-20T09:34:00+08:00",
                "unit": "C",
                "value": "1148",
            }
        ],
        "calibrations": corrected_calibrations(dirty),
    }


def test_boundary_spans_verification_interval_not_just_bad_point(dirty_result):
    plan = dirty_result["intake"]["plan"]
    boundaries = derive_boundaries(dirty_result["calibration"], plan)
    assert len(boundaries) == 1
    boundary = boundaries[0]
    # 起点是失效校准窗口起点，终点是恢复合格读数的 09:36，而不是只罚 09:34。
    assert boundary["zone"] == "南墙"
    assert boundary["seconds"] == 120
    assert boundary["recovery_time"] == "2026-11-20T01:36:00Z"


def test_clean_sample_has_no_boundary(clean_result):
    plan = clean_result["intake"]["plan"]
    assert derive_boundaries(clean_result["calibration"], plan) == []


def test_same_boundary_drives_alignment_and_soak(dirty, dirty_result):
    lockout = build_lockout(dirty_result, dirty)
    boundary = lockout["boundaries"][0]
    # 热区对齐：闭锁窗口内南墙没有任何有效读数带，且带不跨越边界。
    south = lockout["alignment"]["zones"]["南墙"]
    assert all(
        not (span["start"] < boundary["end"] and span["end"] > boundary["start"])
        for span in south
    )
    # 保温计时：同一窗口统一记缺证，原因码为闭锁待补证。
    locked_sections = [
        section
        for section in lockout["soak"]["sections"]
        if LOCK_CODE in section["codes"]
    ]
    assert sum(section["seconds"] for section in locked_sections) == 120
    assert all(section["state"] == "unproven" for section in locked_sections)


def test_lockout_partitions_full_plan_and_keeps_balance(dirty, dirty_result):
    lockout = build_lockout(dirty_result, dirty)
    balance = lockout["reconcile"]
    assert balance["status"] == "MATCHED"
    assert balance["continuous_partition"] is True
    assert balance["planned_s"] == 2520
    assert (
        balance["proven_s"] + balance["cold_s"] + balance["unproven_s"]
        == balance["planned_s"]
    )


def test_locked_batch_cannot_be_signed_off(dirty, dirty_result):
    with pytest.raises(BusinessError, match="复采闭锁"):
        approval(dirty_result, dirty)


def test_resample_passes_and_clears_boundary(dirty):
    verdict = evaluate_resample(dirty, resample_payload(dirty))
    assert verdict["passed"] is True
    assert verdict["lockout"]["boundaries"] == []
    coverage = verdict["coverage"][0]
    assert coverage["zone"] == "南墙" and coverage["complete"] is True
    # 复采后的窗口在正式复算里重新达标，原窗口的缺证被补上。
    section = next(
        item
        for item in verdict["result"]["soak"]["sections"]
        if item["time"] == "2026-11-20T01:34:00Z"
    )
    assert section["state"] == "proven"


def test_resample_must_cover_whole_boundary(dirty):
    payload = resample_payload(dirty)
    # 09:30 在边界（09:34–09:36）之外，既越界又留空，直接拒绝。
    payload["readings"][0]["time"] = "2026-11-20T09:30:00+08:00"
    with pytest.raises(BusinessError):
        evaluate_resample(dirty, payload)


def test_resample_incomplete_when_reading_misses_window(dirty):
    payload = resample_payload(dirty)
    # 两个热区边界场景下只覆盖其中一道会报未覆盖；脏样例单边界则用空读数验证。
    payload["readings"] = []
    with pytest.raises(BusinessError, match="复采读数不能为空"):
        evaluate_resample(dirty, payload)


def test_resample_cannot_touch_trusted_window(dirty):
    payload = resample_payload(dirty)
    payload["readings"].append(
        {
            "zone": "南墙",
            "time": "2026-11-20T09:30:00+08:00",
            "unit": "C",
            "value": "1148",
        }
    )
    with pytest.raises(BusinessError, match="闭锁边界之外"):
        evaluate_resample(dirty, payload)


def test_resample_rejects_unparseable_reading(dirty):
    payload = resample_payload(dirty)
    payload["readings"][0]["time"] = "坏时间"
    with pytest.raises(BusinessError, match="无法采信"):
        evaluate_resample(dirty, payload)


def test_failed_resample_keeps_old_boundary(dirty):
    payload = resample_payload(dirty)
    # 只补读数不重校：漂移校准仍在，闭锁不能解除。
    payload.pop("calibrations")
    verdict = evaluate_resample(dirty, payload)
    assert verdict["passed"] is False
    assert any("校准" in reason for reason in verdict["failures"])
    assert len(verdict["lockout"]["boundaries"]) == 1


def test_retry_does_not_add_boundary_or_retime(dirty):
    first = evaluate_resample(dirty, resample_payload(dirty))
    second = evaluate_resample(dirty, resample_payload(dirty))
    assert [b["start"] for b in first["lockout"]["boundaries"]] == [
        b["start"] for b in second["lockout"]["boundaries"]
    ]
    assert first["resample_sha256"] == second["resample_sha256"]


def test_calibration_overlap_and_gap_lock_full_window(clean):
    overlap = copy.deepcopy(clean)
    overlap["calibrations"].append(
        dict(overlap["calibrations"][0], offset_mc=200)
    )
    result = calculate(overlap)
    overlap_lock = build_lockout(result, overlap)
    assert [
        (item["zone"], item["seconds"]) for item in overlap_lock["boundaries"]
    ] == [("北墙", 2520)]

    gap = copy.deepcopy(clean)
    gap["calibrations"][0]["end"] = gap["plan"]["start"]
    gap_result = calculate(gap)
    gap_lock = build_lockout(gap_result, gap)
    north = next(item for item in gap_lock["boundaries"] if item["zone"] == "北墙")
    assert north["seconds"] == 2520 and north["recovery"] is None
    assert north["end"] == gap_result["intake"]["plan"]["end"]


def test_boundary_merges_adjacent_fault_windows(clean):
    # 南墙两张首尾相接的超限校准应合并为一道边界。
    clean["calibrations"] = [
        row
        for row in clean["calibrations"]
        if row["zone"] != "南墙"
    ] + [
        {
            "zone": "南墙",
            "start": "2026-11-20T09:10:00+08:00",
            "end": "2026-11-20T09:20:00+08:00",
            "offset_mc": 4000,
        },
        {
            "zone": "南墙",
            "start": "2026-11-20T09:20:00+08:00",
            "end": "2026-11-20T09:30:00+08:00",
            "offset_mc": 4100,
        },
        {
            "zone": "南墙",
            "start": "2026-11-20T09:30:00+08:00",
            "end": "2026-11-20T09:50:00+08:00",
            "offset_mc": 1100,
        },
    ]
    result = calculate(clean)
    boundaries = derive_boundaries(
        result["calibration"], result["intake"]["plan"]
    )
    south = [item for item in boundaries if item["zone"] == "南墙"]
    # 09:00–09:10 校准缺口与 09:10–09:30 两张相接超限窗合并为一道边界，
    # 恢复点是新台账生效后的首个合格读数 09:30。
    assert len(south) == 1 and south[0]["seconds"] == 1800
    assert south[0]["start"] == result["intake"]["plan"]["start"]
    assert south[0]["recovery_time"] == "2026-11-20T01:30:00Z"
