import asyncio
import json

from pydantic import BaseModel
from zenode import Node, Service, Topic, TopicSet, every, publish, serve
from zenode.testing import harness, local_transport

from zenode_monitor.bus import MonitorBus
from zenode_monitor.profiles import Profile, ProfileBook
from zenode_monitor.state import Store


class Num(BaseModel):
    value: float = 0.0


class T(TopicSet):
    ticks = Topic("demo/ticks", Num)
    state = Topic("demo/state", Num, latched=True)
    double = Service("demo/double", request=Num, reply=Num, description="Doubles it.")


class Demo(Node):
    name = "demo"
    health_interval = 0.2
    ticks = publish(T.ticks)
    status_pub = publish(T.state)

    async def on_start(self) -> None:
        self.status_pub.put(Num(value=7))

    @every(0.05)
    async def tick(self) -> None:
        self.ticks.put(Num(value=1))

    @serve(T.double)
    async def on_double(self, req: Num) -> Num:
        if req.value < 0:
            raise ValueError("negative")
        return Num(value=req.value * 2)


async def until(cond, timeout=5.0):
    async with asyncio.timeout(timeout):
        while not cond():
            await asyncio.sleep(0.02)


async def test_monitor_sees_a_node_end_to_end():
    async with harness("t") as h:
        await h.start_node(Demo)
        store = Store()
        bus = MonitorBus(store)
        await bus.connect(local_transport("t"), session=h.session)
        try:
            await until(lambda: "demo" in store.nodes and store.nodes["demo"].info is not None)
            await until(lambda: store.nodes["demo"].latest("sent") is not None)
            node = store.nodes["demo"]
            assert node.status == "running"
            assert node.latest("sent") > 0
            assert "t/demo/ticks" in store.topics and store.topics["t/demo/state"].latched
            svc = store.services["t/demo/double"]
            assert svc.info.request_schema["properties"]["value"]["type"] == "number"

            bus.watch_topic("t/demo/ticks")
            await until(lambda: store.stream.count >= 3)
            sample = store.stream.samples[0]
            assert sample.is_json and sample.envelope.node == "demo"
            assert store.stream.rate()[0] > 0

            bus.watch_topic("t/demo/state", latched=True)  # published once, before we joined
            await until(lambda: store.stream.count >= 1)
            assert json.loads(store.stream.samples[0].payload) == {"value": 7.0}
            bus.unwatch_topic()

            ok = await bus.call("t/demo/double", b'{"value": 4}')
            assert ok.ok and json.loads(ok.text) == {"value": 8.0}
            bad = await bus.call("t/demo/double", b'{"value": -1}')
            assert not bad.ok and bad.kind == "error" and "negative" in bad.text
            invalid = await bus.call("t/demo/double", b'{"value": "x"}')
            assert not invalid.ok and "bad request" in invalid.text
            gone = await bus.call("t/demo/nope", b"{}", timeout=0.3)
            assert gone.kind == "timeout"
        finally:
            await bus.disconnect()
        assert not bus.connected


def test_profiles_round_trip(tmp_path):
    path = tmp_path / "p.json"
    book = ProfileBook()
    book.put(Profile(name="rover", mode="client", connect=["tcp/10.0.0.5:7447"], namespace="rover"))
    book.put(Profile(name="rover", namespace="/rover2/"))
    book.last = "rover"
    book.save(path)
    loaded = ProfileBook.load(path)
    assert [p.name for p in loaded.profiles] == ["rover"] and loaded.last == "rover"
    assert loaded.get("rover").to_transport().namespace == "rover2"
    assert ProfileBook.load(tmp_path / "missing.json").profiles == []


def test_profile_from_toml(tmp_path):
    cfg = tmp_path / "zenode.toml"
    cfg.write_text('[transport]\nnamespace = "rover"\nshared_memory = true\n')
    p = Profile.from_toml(str(cfg))
    assert p.namespace == "rover" and p.shared_memory and p.mode == "peer"
