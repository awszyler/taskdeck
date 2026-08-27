import pytest
from taskdeck_core.crp.hub import RunnerConnection, RunnerHub


class _FakeSocket:
    def __init__(self):
        self.sent: list[dict] = []

    async def send_json(self, data: dict) -> None:
        self.sent.append(data)


@pytest.mark.asyncio
async def test_register_and_pick_idle():
    hub = RunnerHub()
    sock = _FakeSocket()
    conn = RunnerConnection(
        runner_id="r-1",
        socket=sock,  # type: ignore[arg-type]
        max_parallel=2,
        capabilities=["shell"],
    )
    hub.register(conn)
    picked = hub.pick_for("shell")
    assert picked is conn


@pytest.mark.asyncio
async def test_pick_returns_none_when_saturated():
    hub = RunnerHub()
    sock = _FakeSocket()
    conn = RunnerConnection(
        runner_id="r-1",
        socket=sock,  # type: ignore[arg-type]
        max_parallel=1,
        capabilities=["shell"],
    )
    hub.register(conn)
    conn.increment_inflight()
    assert hub.pick_for("shell") is None


@pytest.mark.asyncio
async def test_pick_least_loaded():
    hub = RunnerHub()
    a = RunnerConnection("a", _FakeSocket(), 4, ["shell"])  # type: ignore[arg-type]
    b = RunnerConnection("b", _FakeSocket(), 4, ["shell"])  # type: ignore[arg-type]
    hub.register(a)
    hub.register(b)
    a.increment_inflight()
    a.increment_inflight()
    picked = hub.pick_for("shell")
    assert picked is b


@pytest.mark.asyncio
async def test_pick_filters_by_capability():
    hub = RunnerHub()
    a = RunnerConnection("a", _FakeSocket(), 4, ["shell"])  # type: ignore[arg-type]
    hub.register(a)
    assert hub.pick_for("claude-code") is None


@pytest.mark.asyncio
async def test_unregister_removes():
    hub = RunnerHub()
    sock = _FakeSocket()
    conn = RunnerConnection("r-1", sock, 4, ["shell"])  # type: ignore[arg-type]
    hub.register(conn)
    hub.unregister("r-1")
    assert hub.pick_for("shell") is None


def test_available_capabilities_dedupes_across_runners():
    hub = RunnerHub()
    a = RunnerConnection(
        "a", _FakeSocket(), 4, ["shell", "claude-code"],  # type: ignore[arg-type]
        capability_descriptions={"shell": "S", "claude-code": "CC"},
    )
    b = RunnerConnection(
        "b", _FakeSocket(), 4, ["shell", "kiro-cli"],  # type: ignore[arg-type]
        capability_descriptions={"shell": "S2", "kiro-cli": "KC"},
    )
    hub.register(a)
    hub.register(b)

    out = hub.available_capabilities()
    by_cap = {x["capability"]: x["description"] for x in out}
    assert set(by_cap.keys()) == {"shell", "claude-code", "kiro-cli"}
    # First-registered description wins on dedup; this is fine — descriptions
    # are conventionally identical across runners that publish the same cap.
    assert by_cap["claude-code"] == "CC"
    assert by_cap["kiro-cli"] == "KC"


def test_available_capabilities_empty_when_no_runners():
    hub = RunnerHub()
    assert hub.available_capabilities() == []


def test_available_capabilities_keeps_missing_description_as_empty_string():
    hub = RunnerHub()
    conn = RunnerConnection("r-old", _FakeSocket(), 1, ["shell"])  # type: ignore[arg-type]
    hub.register(conn)
    out = hub.available_capabilities()
    assert out == [{"capability": "shell", "description": ""}]
