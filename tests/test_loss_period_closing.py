"""停机损失期间结账（POST /devices/{id}/loss-period-closings、
GET /loss-period-closings/{closing_no}、GET /devices/{id}/loss-period-closings）
端到端测试。

覆盖：期间内起始且截至序号已结算班次的选取、每班取序号前最后修订、
形成或复用损失账、按原因汇总固化、空结账、同单号幂等/异输入冲突、
序号越界/未知设备/未知费率版本拒绝、缺固化明细整单失败（不留部分新账）、
历史结账不可改、查询接口与并发同单号唯一。

运行方式：docker compose exec app pytest tests/test_loss_period_closing.py
"""

import threading

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.db import engine
from app.main import app

TABLES = (
    "loss_period_closing_lines",
    "loss_period_closings",
    "loss_cost_allocation_splits",
    "loss_cost_allocation_accounts",
    "loss_cost_allocations",
    "loss_accounting_pieces",
    "loss_accountings",
    "rate_entries",
    "rate_versions",
    "manual_adjudications",
    "shift_result_segments",
    "shift_results",
    "event_versions",
    "event_sources",
    "shifts",
    "devices",
    "change_counter",
)

DEVICE = {"id": "CNC-01", "name": "加工中心1号"}


@pytest.fixture()
def client():
    with TestClient(app) as c:  # 触发 lifespan：建库建表与迁移
        with engine.begin() as conn:
            for t in TABLES:
                conn.execute(text(f"TRUNCATE {t} CASCADE"))
        yield c


# ---------------------------------------------------------------- 构造数据


def _shift(client, start: str, end: str, device="CNC-01") -> int:
    r = client.post(
        "/shifts", json={"device_id": device, "start": start, "end": end}
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _event(client, source, version, start, end, reason, device="CNC-01"):
    r = client.post(
        "/events",
        json={
            "device_id": device,
            "source_id": source,
            "version": version,
            "start": start,
            "end": end,
            "reason": reason,
        },
    )
    assert r.status_code == 201, r.text


def _settle(client, shift_id):
    r = client.post(f"/shifts/{shift_id}/settle")
    assert r.status_code == 200, r.text


def _publish(client, entries, device="CNC-01"):
    r = client.post(
        f"/devices/{device}/rate-versions", json={"entries": entries}
    )
    assert r.status_code == 201, r.text
    return r.json()["version"]


def _close(client, body, device="CNC-01", expect=None):
    r = client.post(f"/devices/{device}/loss-period-closings", json=body)
    if expect is not None:
        assert r.status_code == expect, r.text
    return r


def _full_rates(start_day="2026-10-01T00:00:00", end_day="2026-10-04T00:00:00"):
    return [
        {"reason": "failure", "start": f"{start_day}+08:00",
         "end": f"{end_day}+08:00", "amount_per_minute": "1"},
        {"reason": "maintenance", "start": f"{start_day}+08:00",
         "end": f"{end_day}+08:00", "amount_per_minute": "2"},
        {"reason": "pending", "start": f"{start_day}+08:00",
         "end": f"{end_day}+08:00", "amount_per_minute": "0"},
    ]


def _scenario(client):
    """构造主场景（seq 见注释）：

    - 班次 A：10/1 08:00-16:00，故障 10:00-12:00 + 保养 11:00-11:30
      → 结算 rev1（seq=1：故障 90 / 保养 30）；更正故障缩短为 10:00-11:00
      → rev2（seq=2：故障 60 / 保养 30）；
    - 班次 B：10/2 08:00-16:00，故障 09:00-10:00 → 结算 rev1（seq=3：故障 60）；
    - 班次 C：10/2 16:00-24:00，期间内起始但未结算（不纳入）；
    - 班次 D：10/3 08:00-16:00，期间外起始（不纳入，即便已结算）。
    """
    client.post("/devices", json=DEVICE)
    shift_a = _shift(client, "2026-10-01T08:00:00+08:00", "2026-10-01T16:00:00+08:00")
    _event(client, "PLC-9", 1, "2026-10-01T10:00:00+08:00",
           "2026-10-01T12:00:00+08:00", "failure")
    _event(client, "MES-1", 1, "2026-10-01T11:00:00+08:00",
           "2026-10-01T11:30:00+08:00", "maintenance")
    _settle(client, shift_a)  # seq=1
    _event(client, "PLC-9", 2, "2026-10-01T10:00:00+08:00",
           "2026-10-01T11:00:00+08:00", "failure")  # seq=2

    shift_b = _shift(client, "2026-10-02T08:00:00+08:00", "2026-10-02T16:00:00+08:00")
    _event(client, "MES-2", 1, "2026-10-02T09:00:00+08:00",
           "2026-10-02T10:00:00+08:00", "failure")
    _settle(client, shift_b)  # seq=3

    shift_c = _shift(client, "2026-10-02T16:00:00+08:00", "2026-10-03T00:00:00+08:00")
    shift_d = _shift(client, "2026-10-03T08:00:00+08:00", "2026-10-03T16:00:00+08:00")
    _event(client, "MES-3", 1, "2026-10-03T09:00:00+08:00",
           "2026-10-03T10:00:00+08:00", "failure")
    _settle(client, shift_d)  # seq=4（期间外起始，不纳入）

    _publish(client, _full_rates())  # 费率版本 1
    return shift_a, shift_b, shift_c, shift_d


CLOSING_BODY = {
    "device_id": "CNC-01",
    "closing_no": "PC-202610-001",
    "start": "2026-10-01T00:00:00+08:00",
    "end": "2026-10-03T00:00:00+08:00",
    "seq": 3,
    "rate_version": 1,
}


# ---------------------------------------------------------------- 基本结账


def test_closing_basic_selects_last_revision_at_seq(client):
    shift_a, shift_b, _c, _d = _scenario(client)

    r = _close(client, CLOSING_BODY, expect=201)
    data = r.json()
    assert data["idempotent"] is False
    assert data["closing_no"] == "PC-202610-001"
    assert data["device_id"] == "CNC-01"
    assert data["seq"] == 3 and data["rate_version"] == 1
    assert data["shift_count"] == 2

    # 班次 A 取 seq<=3 的最后修订 rev2；班次 B 取 rev1；未结算的 C 与期间外的 D 不纳入
    lines = data["lines"]
    assert [(ln["line_no"], ln["shift_id"], ln["revision"]) for ln in lines] == [
        (1, shift_a, 2),
        (2, shift_b, 1),
    ]
    # 班次 A：故障 60×1 + 保养 30×2 = 120.00；班次 B：故障 60×1 = 60.00
    assert lines[0]["totals"]["amount"] == "120.00"
    assert lines[1]["totals"]["amount"] == "60.00"
    # 合计按原因汇总
    totals = data["totals"]
    assert totals["amount"] == "180.00"
    assert totals["amount_cents"] == 18000
    assert totals["priced_minutes"] == 150
    assert totals["unpriced_minutes"] == 0
    by = {x["reason"]: x for x in totals["by_reason"]}
    assert by["failure"]["priced_minutes"] == 120
    assert by["failure"]["amount"] == "120.00"
    assert by["maintenance"]["priced_minutes"] == 30
    assert by["maintenance"]["amount"] == "60.00"
    assert by["pending"]["amount"] == "0.00"

    # 账引用：为两个组合各形成一份损失账（可经既有接口取回）
    acc_a = client.get(f"/shifts/{shift_a}/loss-accountings").json()
    acc_b = client.get(f"/shifts/{shift_b}/loss-accountings").json()
    assert [(x["revision"], x["rate_version"]) for x in acc_a] == [(2, 1)]
    assert [(x["revision"], x["rate_version"]) for x in acc_b] == [(1, 1)]
    assert lines[0]["accounting_id"] == acc_a[0]["id"]
    assert lines[1]["accounting_id"] == acc_b[0]["id"]

    # 结账不产生归因变更（不是修订）
    changes = client.get("/changes?cursor=0&limit=500").json()["changes"]
    assert [c["seq"] for c in changes] == [1, 2, 3, 4]


def test_closing_seq_selects_earlier_revision_and_unsettled_shifts(client):
    shift_a, shift_b, _c, _d = _scenario(client)

    # seq=1：班次 B 尚未结算（其 rev1 为 seq=3），只纳入班次 A 的 rev1
    body = {**CLOSING_BODY, "closing_no": "PC-SEQ-1", "seq": 1}
    data = _close(client, body, expect=201).json()
    assert data["shift_count"] == 1
    assert [(ln["shift_id"], ln["revision"]) for ln in data["lines"]] == [
        (shift_a, 1)
    ]
    # rev1：故障 90×1 + 保养 30×2 = 150.00
    assert data["totals"]["amount"] == "150.00"

    # seq=2：班次 A 取 rev2（故障 60），班次 B 仍未结算
    body = {**CLOSING_BODY, "closing_no": "PC-SEQ-2", "seq": 2}
    data = _close(client, body, expect=201).json()
    assert [(ln["shift_id"], ln["revision"]) for ln in data["lines"]] == [
        (shift_a, 2)
    ]
    assert data["totals"]["amount"] == "120.00"


def test_closing_shift_start_boundary(client):
    """期间内起始按半开判定：起点落在期间内即纳入，恰在期间终点起始不纳入。"""
    client.post("/devices", json=DEVICE)
    # X：09/30 16:00 - 10/1 00:00（期间前起始）→ 不纳入
    sx = _shift(client, "2026-09-30T16:00:00+08:00", "2026-10-01T00:00:00+08:00")
    _event(client, "S-X", 1, "2026-09-30T18:00:00+08:00",
           "2026-09-30T19:00:00+08:00", "failure")
    _settle(client, sx)
    # Y：10/1 00:00 - 08:00（恰在期间起点起始）→ 纳入
    sy = _shift(client, "2026-10-01T00:00:00+08:00", "2026-10-01T08:00:00+08:00")
    _event(client, "S-Y", 1, "2026-10-01T02:00:00+08:00",
           "2026-10-01T03:00:00+08:00", "failure")
    _settle(client, sy)
    # Z：10/3 00:00 - 08:00（恰在期间终点起始）→ 不纳入
    sz = _shift(client, "2026-10-03T00:00:00+08:00", "2026-10-03T08:00:00+08:00")
    _event(client, "S-Z", 1, "2026-10-03T02:00:00+08:00",
           "2026-10-03T03:00:00+08:00", "failure")
    _settle(client, sz)
    _publish(client, _full_rates())

    body = {**CLOSING_BODY, "closing_no": "PC-BOUND", "seq": 3}
    data = _close(client, body, expect=201).json()
    assert [ln["shift_id"] for ln in data["lines"]] == [sy]
    assert data["totals"]["amount"] == "60.00"  # 故障 60×1


def test_closing_empty_when_no_qualifying_shifts(client):
    client.post("/devices", json=DEVICE)
    _publish(client, _full_rates())
    # 无任何班次 → 空结账（seq=0 合法：尚无结算）
    data = _close(client, CLOSING_BODY | {"seq": 0}, expect=201).json()
    assert data["shift_count"] == 0
    assert data["lines"] == []
    assert data["totals"]["amount"] == "0.00"
    assert data["totals"]["priced_minutes"] == 0
    assert data["totals"]["unpriced_minutes"] == 0
    by = {x["reason"]: x for x in data["totals"]["by_reason"]}
    assert set(by) == {"maintenance", "failure", "pending"}
    assert all(x["amount"] == "0.00" for x in by.values())

    # 有班次但截至序号均未结算 → 同样空结账
    _scenario(client)
    body = {**CLOSING_BODY, "closing_no": "PC-EMPTY-2", "seq": 0}
    data = _close(client, body, expect=201).json()
    assert data["shift_count"] == 0 and data["lines"] == []


def test_closing_unpriced_minutes_aggregated(client):
    """费率缺口分钟单列待定价并汇入结账汇总，绝不按零金额计。"""
    shift_a, shift_b, _c, _d = _scenario(client)
    # 费率版本 2：只有故障费率 → 保养 30 分钟待定价
    _publish(
        client,
        [{"reason": "failure", "start": "2026-10-01T00:00:00+08:00",
          "end": "2026-10-04T00:00:00+08:00", "amount_per_minute": "1"}],
    )
    body = {**CLOSING_BODY, "closing_no": "PC-UNPRICED", "rate_version": 2}
    data = _close(client, body, expect=201).json()
    totals = data["totals"]
    assert totals["amount"] == "120.00"  # 故障 60+60
    assert totals["priced_minutes"] == 120
    assert totals["unpriced_minutes"] == 30
    by = {x["reason"]: x for x in totals["by_reason"]}
    assert by["maintenance"]["unpriced_minutes"] == 30
    assert by["maintenance"]["amount"] == "0.00"
    line_a = next(ln for ln in data["lines"] if ln["shift_id"] == shift_a)
    assert line_a["totals"]["unpriced_minutes"] == 30


# ---------------------------------------------------------------- 幂等与冲突


def test_closing_idempotent_retry_and_conflict(client):
    _scenario(client)
    first = _close(client, CLOSING_BODY, expect=201).json()

    # 同单号同输入重试 → 200 幂等，内容逐字段一致（含账引用）
    again = _close(client, CLOSING_BODY, expect=200).json()
    assert again["idempotent"] is True
    assert again["id"] == first["id"]
    assert again["created_at"] == first["created_at"]
    assert again["lines"] == first["lines"]
    assert again["totals"] == first["totals"]

    # 同一瞬间的不同时区写法视为同输入（期间端点归一 UTC 后判定）
    same_instant = {
        **CLOSING_BODY,
        "start": "2026-09-30T16:00:00Z",
        "end": "2026-10-02T16:00:00Z",
    }
    retry = _close(client, same_instant, expect=200).json()
    assert retry["id"] == first["id"] and retry["idempotent"] is True

    # 同单号异输入（改序号 / 改费率版本 / 改期间）→ 409 CLOSING_CONFLICT
    for patch in ({"seq": 2}, {"rate_version": 1, "seq": 3, "end": "2026-10-04T00:00:00+08:00"}):
        r = _close(client, {**CLOSING_BODY, **patch})
        assert r.status_code == 409, r.text
        assert r.json()["detail"]["code"] == "CLOSING_CONFLICT"

    # 冲突后原结账不变；库中仍只有一单
    got = client.get(f"/loss-period-closings/{CLOSING_BODY['closing_no']}").json()
    assert got["lines"] == first["lines"]
    with engine.begin() as conn:
        n = conn.execute(text("SELECT count(*) FROM loss_period_closings")).scalar()
    assert n == 1


def test_closing_reuses_existing_accounting(client):
    """已固化的 (班次, 修订, 费率版本) 账被结账复用，不重复产生。"""
    shift_a, shift_b, _c, _d = _scenario(client)
    # 预先核算班次 A 的 rev2 × 费率版本 1
    pre = client.post(
        f"/shifts/{shift_a}/loss-accountings",
        json={"rate_version": 1, "revision": 2},
    )
    assert pre.status_code == 201, pre.text
    pre_id = pre.json()["id"]

    data = _close(client, CLOSING_BODY, expect=201).json()
    line_a = next(ln for ln in data["lines"] if ln["shift_id"] == shift_a)
    assert line_a["accounting_id"] == pre_id  # 复用既有账
    with engine.begin() as conn:
        n = conn.execute(
            text(
                "SELECT count(*) FROM loss_accountings "
                "WHERE shift_id=:s AND revision=2 AND rate_version=1"
            ),
            {"s": shift_a},
        ).scalar()
    assert n == 1


# ---------------------------------------------------------------- 明确拒绝


def test_closing_seq_out_of_range(client):
    _scenario(client)  # 最大序号为 4
    r = _close(client, {**CLOSING_BODY, "seq": 99})
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "INVALID_SEQ"
    with engine.begin() as conn:
        n = conn.execute(text("SELECT count(*) FROM loss_period_closings")).scalar()
    assert n == 0


def test_closing_unknown_device_and_rate_version(client):
    _scenario(client)
    r = _close(client, {**CLOSING_BODY, "device_id": "NOPE"}, device="NOPE")
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "DEVICE_NOT_FOUND"

    r = _close(client, {**CLOSING_BODY, "rate_version": 99})
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "RATE_VERSION_NOT_FOUND"

    # 其他设备的费率版本不能用于本设备结账
    client.post("/devices", json={"id": "CNC-02", "name": "加工中心2号"})
    _publish(client, _full_rates(), device="CNC-02")  # CNC-02 的版本 1
    client.post("/devices", json={"id": "CNC-03", "name": "加工中心3号"})
    r = _close(
        client, {**CLOSING_BODY, "device_id": "CNC-03", "rate_version": 1},
        device="CNC-03",
    )
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "RATE_VERSION_NOT_FOUND"


def test_closing_segments_unavailable_fails_atomically(client):
    """任一所选修订缺固化明细 → 整单失败：不留结账，也不留部分新账。"""
    shift_a, shift_b, _c, _d = _scenario(client)
    # 班次 A 的 rev2 模拟为结构引入前的历史修订（无固化明细）
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE shift_results SET segments_available = FALSE "
                "WHERE shift_id = :s AND revision = 2"
            ),
            {"s": shift_a},
        )
    r = _close(client, CLOSING_BODY)
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "SEGMENTS_UNAVAILABLE"

    # 整单回滚：无结账记录，且班次 B 的账也未留下（不留部分新账）
    with engine.begin() as conn:
        assert conn.execute(
            text("SELECT count(*) FROM loss_period_closings")
        ).scalar() == 0
        assert conn.execute(
            text("SELECT count(*) FROM loss_period_closing_lines")
        ).scalar() == 0
        assert conn.execute(
            text("SELECT count(*) FROM loss_accountings")
        ).scalar() == 0

    # 仅班次 B 落在期间内时（期间只覆盖 10/2）可正常结账：验证失败确由
    # 班次 A 的无明细修订引起
    body = {
        **CLOSING_BODY,
        "closing_no": "PC-ONLY-B",
        "start": "2026-10-02T00:00:00+08:00",
    }
    data = _close(client, body, expect=201).json()
    assert [ln["shift_id"] for ln in data["lines"]] == [shift_b]


def test_closing_validation_errors(client):
    _scenario(client)
    base = CLOSING_BODY
    # 缺时区 / 未对齐整分钟 / 期间倒置
    for patch in (
        {"start": "2026-10-01T00:00:00"},
        {"start": "2026-10-01T00:00:30+08:00"},
        {"start": "2026-10-03T00:00:00+08:00", "end": "2026-10-01T00:00:00+08:00"},
        {"seq": -1},
        {"rate_version": 0},
        {"closing_no": ""},
    ):
        r = _close(client, {**base, **patch})
        assert r.status_code == 422, (patch, r.text)
    # body 设备与路径不一致
    r = _close(client, {**base, "device_id": "CNC-99"})
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "VALIDATION_ERROR"


# ---------------------------------------------------------------- 历史不可改


def test_closing_history_immutable(client):
    """后续事件更正、费率发布和重新核算都不改变历史结账。"""
    shift_a, shift_b, _c, _d = _scenario(client)
    first = _close(client, CLOSING_BODY, expect=201).json()

    # 后续：事件更正（班次 A 产生 rev3）、新费率版本、新组合核算
    _event(client, "PLC-9", 3, "2026-10-01T10:00:00+08:00",
           "2026-10-01T10:30:00+08:00", "failure")  # seq=5
    _publish(client, _full_rates())  # 费率版本 2
    r = client.post(
        f"/shifts/{shift_a}/loss-accountings",
        json={"rate_version": 2, "revision": 3},
    )
    assert r.status_code == 201, r.text

    # 重读结账：内容与原固化完全一致
    got = client.get(f"/loss-period-closings/{CLOSING_BODY['closing_no']}")
    assert got.status_code == 200
    assert got.json()["lines"] == first["lines"]
    assert got.json()["totals"] == first["totals"]
    assert got.json()["seq"] == 3

    # 同输入重试仍返回原结账（不会因新修订/新费率而重算）
    again = _close(client, CLOSING_BODY, expect=200).json()
    assert again["id"] == first["id"]
    assert again["lines"] == first["lines"]


# ---------------------------------------------------------------- 查询


def test_closing_get_and_list(client):
    _scenario(client)
    first = _close(client, CLOSING_BODY, expect=201).json()
    body2 = {**CLOSING_BODY, "closing_no": "PC-202610-002", "seq": 1}
    second = _close(client, body2, expect=201).json()

    # 按单号读取（含班次行明细）
    got = client.get("/loss-period-closings/PC-202610-001")
    assert got.status_code == 200
    data = got.json()
    assert data["id"] == first["id"]
    assert data["idempotent"] is True
    assert len(data["lines"]) == 2
    assert data["lines"][0]["accounting_id"] == first["lines"][0]["accounting_id"]

    # 未知单号 → 404 CLOSING_NOT_FOUND
    r = client.get("/loss-period-closings/NOPE")
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "CLOSING_NOT_FOUND"

    # 按设备列出（按提交先后）
    listing = client.get("/devices/CNC-01/loss-period-closings")
    assert listing.status_code == 200
    assert [c["closing_no"] for c in listing.json()] == [
        "PC-202610-001",
        "PC-202610-002",
    ]
    assert listing.json()[1]["shift_count"] == 1

    # 未知设备列表 → 404
    r = client.get("/devices/NOPE/loss-period-closings")
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "DEVICE_NOT_FOUND"


# ---------------------------------------------------------------- 并发


def test_concurrent_same_closing_no_single_order(client):
    _scenario(client)
    barrier = threading.Barrier(4)
    statuses: list[int] = []
    errors: list[Exception] = []
    lock = threading.Lock()

    def worker():
        try:
            barrier.wait(timeout=10)
            r = client.post(
                "/devices/CNC-01/loss-period-closings", json=CLOSING_BODY
            )
            with lock:
                statuses.append(r.status_code)
                assert r.status_code in (200, 201), r.text
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert sorted(statuses).count(201) == 1
    assert sorted(statuses).count(200) == 3
    with engine.begin() as conn:
        assert conn.execute(
            text("SELECT count(*) FROM loss_period_closings")
        ).scalar() == 1
        assert conn.execute(
            text("SELECT count(*) FROM loss_period_closing_lines")
        ).scalar() == 2
        # 两个班次组合各只有一份账（并发未产生重复账）
        assert conn.execute(
            text("SELECT count(*) FROM loss_accountings")
        ).scalar() == 2
