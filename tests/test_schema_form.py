import json
from enum import Enum

from pydantic import BaseModel, Field
from zenode.msgs import Empty

from zenode_monitor.schema_form import build_payload, fields_from_schema, skeleton


class Mode(str, Enum):
    slow = "slow"
    fast = "fast"


class Inner(BaseModel):
    a: int = 0


class Req(BaseModel):
    speed: float = Field(ge=0, le=2, description="m/s")
    count: int = 3
    label: str | None = None
    on: bool = False
    mode: Mode = Mode.slow
    inner: Inner = Inner()
    values: list[float] = []


def test_fields():
    f = {x.name: x for x in fields_from_schema(Req.model_json_schema())}
    assert f["speed"].kind == "number" and f["speed"].required
    assert f["speed"].hint() == "0 to 2 · m/s"
    assert f["count"].kind == "integer" and f["count"].initial() == "3"
    assert f["label"].kind == "string" and f["label"].nullable
    assert f["on"].kind == "boolean" and f["on"].initial() is False
    assert f["mode"].kind == "enum" and f["mode"].choices == ["slow", "fast"]
    assert f["inner"].kind == "json" and f["values"].kind == "json"


def test_build_payload_round_trips_through_the_model():
    fields = fields_from_schema(Req.model_json_schema())
    payload, errors = build_payload(fields, {
        "speed": "1.5", "count": "", "label": "", "on": True, "mode": "fast",
        "inner": '{"a": 2}', "values": "[1, 2]",
    })
    assert errors == {}
    req = Req.model_validate(payload)
    assert (req.speed, req.count, req.on, req.mode, req.inner.a, req.values) == (1.5, 3, True, Mode.fast, 2, [1, 2])


def test_errors():
    fields = fields_from_schema(Req.model_json_schema())
    _, errors = build_payload(fields, {"speed": "", "count": "x", "inner": "{"})
    assert errors == {"speed": "Required", "count": "Enter a whole number", "inner": "Enter valid JSON"}


def test_empty_schema_and_skeleton():
    assert fields_from_schema({}) == []
    assert fields_from_schema(Empty.model_json_schema()) == []
    sk = json.loads(skeleton(Req.model_json_schema()))
    assert sk["speed"] == 0.0 and sk["count"] == 3 and sk["mode"] == "slow"
