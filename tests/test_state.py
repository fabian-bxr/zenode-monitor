import json
import time

from zenode.envelope import Envelope
from zenode.msgs import EntityInfo, MeasureDescriptor, NodeHealth, NodeInfo, ServiceInfo

from zenode_monitor import state
from zenode_monitor.state import Store, TopicSample


def health(node="a", **kw) -> bytes:
    base = dict(node=node, state="running", uptime_s=1.0, ts_ns=time.time_ns())
    return NodeHealth(**{**base, **kw}).model_dump_json().encode()


def info(node="a", **kw) -> bytes:
    return NodeInfo(node=node, **kw).model_dump_json().encode()


def test_presence_and_status():
    s = Store()
    s.on_presence("a", True)
    assert s.nodes["a"].status == state.ONLINE
    s.on_health(health(state="starting"))
    assert s.nodes["a"].status == "starting"
    s.on_presence("a", False)
    assert s.nodes["a"].status == state.OFFLINE
    assert [n.text for n in s.take_notices()] == ["Node a joined", "Node a left"]


def test_no_join_notices_while_settling():
    s = Store()
    s.reset("ns")
    s.on_presence("a", True)
    assert s.take_notices() == []


def test_counter_becomes_rate_and_restart_is_skipped(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(state.time, "monotonic", lambda: clock[0])
    s = Store()
    s.on_presence("a", True)
    s.on_health(health(sent=10, cpu_percent=5.0))
    node = s.nodes["a"]
    assert node.latest("sent") is None  # no interval yet
    assert node.latest("cpu_percent") == 5.0
    clock[0] += 2.0
    s.on_health(health(sent=30))
    assert node.latest("sent") == 10.0
    clock[0] += 2.0
    s.on_health(health(sent=4))  # restarted
    assert len(node.history["sent"]) == 1


def test_custom_measures_use_descriptor(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(state.time, "monotonic", lambda: clock[0])
    s = Store()
    s.on_presence("a", True)
    s.on_info(info(measures=[MeasureDescriptor(id="frames", kind="counter"), MeasureDescriptor(id="speed", unit="m/s")]))
    s.on_health(health(measures={"frames": 0, "speed": 0.4}))
    clock[0] += 1.0
    s.on_health(health(measures={"frames": 30, "speed": 0.5}))
    node = s.nodes["a"]
    assert node.latest("frames") == 30.0
    assert node.latest("speed") == 0.5
    assert state.fmt_metric(node, "speed") == ("0.50", "m/s")


def test_silent_detection_and_recovery(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(state.time, "monotonic", lambda: clock[0])
    s = Store()
    s.on_presence("a", True)
    s.on_info(info(health_interval=1.0))
    s.on_health(health())
    s.take_notices()
    clock[0] = 2.9
    s.tick()
    assert s.nodes["a"].status == "running"
    clock[0] = 3.1
    s.tick()
    assert s.nodes["a"].status == state.SILENT
    s.on_health(health())
    assert s.nodes["a"].status == "running"
    assert len(s.take_notices()) == 2


def test_topic_and_service_index():
    s = Store()
    s.on_presence("pub", True)
    s.on_presence("sub", True)
    s.on_info(info("pub", publishes=[
        EntityInfo(key="ns/sensors/front/range", schema_name="Range", flags=["latched(1)"]),
        EntityInfo(key="ns/node/pub/health", schema_name="NodeHealth"),
    ], serves=[ServiceInfo(key="ns/reset", request="Empty", reply="Pose")]))
    s.on_info(info("sub", subscribes=[
        EntityInfo(key="ns/sensors/*/range", schema_name="Range"),
        EntityInfo(key="ns/nobody", schema_name="X"),
    ]))
    t = s.topics
    assert t["ns/sensors/front/range"].publishers == {"pub"}
    assert t["ns/sensors/front/range"].latched
    assert t["ns/node/pub/health"].system
    assert t["ns/sensors/*/range"].matched and t["ns/sensors/*/range"].wildcard
    assert not t["ns/nobody"].matched
    assert t["ns/sensors/*/range"].publishers == {"pub"}
    assert t["ns/sensors/front/range"].subscribers == {"sub"}
    assert s.services["ns/reset"].nodes == {"pub"}
    s.on_presence("pub", False)  # a node that left no longer contributes
    assert "ns/reset" not in s.services
    assert not s.topics["ns/sensors/*/range"].matched


def test_stream_pause_and_preview():
    s = Store()
    s.stream.reset("k")
    def sample(payload, enc="application/json"):
        return TopicSample(time.time_ns(), "k", payload, enc, Envelope(node="n", seq=1))
    s.on_sample(sample(b'{"x": 1}'))
    s.stream.paused = True
    s.on_sample(sample(b"\x00\x01", "application/octet-stream"))
    assert len(s.stream.samples) == 1 and s.stream.count == 2
    assert s.stream.rate()[0] == 1.0
    assert json.loads(s.stream.samples[0].pretty()) == {"x": 1}
    assert sample(b"\x00\x01", "application/octet-stream").preview() == "2 B  00 01"


def test_formatters():
    assert state.fmt_duration(59) == "59s"
    assert state.fmt_duration(3725) == "1h 02m"
    assert state.fmt_bytes(1536) == "1.5 kB"
    assert state.fmt_unit("{message}") == "msg"
    assert state.hexdump(b"AB").startswith("000000  41 42")


def test_stream_releases_old_large_payloads(monkeypatch):
    monkeypatch.setattr(state, "STREAM_BYTE_BUDGET", 25_000)
    s = Store()
    s.stream.reset("k")
    for i in range(4):
        s.on_sample(TopicSample(i, "k", bytes(10_000), "image/jpeg", Envelope()))
    kept = list(s.stream.samples)
    assert [x.truncated for x in kept] == [True, True, False, False]
    assert len(kept[0].payload) == state.PAYLOAD_HEAD and kept[0].size == 10_000
    assert not kept[0].is_image and kept[-1].is_image
