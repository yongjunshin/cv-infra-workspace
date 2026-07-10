"""CPU unit tests for FU-17: declared-sensor render-product activation.

The pure walk (``enable_sensor_render_products``) is exercised over a fake
USD stage shaped like the measured carter sample graph (p3c1 probe:
``.../ros_lidars`` publish chain complete, gated by ``IsaacCreateRenderProduct``
nodes with ``inputs:enabled=False``). The fakes duck-type ONLY the USD-generic
surface the walk uses (Traverse / GetPrimAtPath / GetAttribute(s) /
GetConnections / Get / Set) — the live-stage wrapper (session-layer edit,
``SimRuntime.enable_declared_sensors``) is GPU scope (T4 proves).

Matching is topicName-driven (contract consumption): node NAMES like
``front_2d_lidar_render_product`` never appear in the code under test — the
graph below deliberately uses different prim names to prove that.
"""

from __future__ import annotations

from cv_infra.runner.sim_runtime import enable_sensor_render_products

RENDER_PRODUCT_TYPE = "isaacsim.core.nodes.IsaacCreateRenderProduct"


class FakeConnection:
    """Stands in for the Sdf property path a Usd connection returns."""

    def __init__(self, prim_path: str):
        self._prim_path = prim_path

    def GetPrimPath(self) -> str:  # noqa: N802 - USD API casing
        return self._prim_path


class FakeAttr:
    def __init__(self, name: str, value=None, connections=()):
        self._name = name
        self._value = value
        self._connections = [FakeConnection(p) for p in connections]
        self.set_calls: list = []

    def GetName(self) -> str:  # noqa: N802
        return self._name

    def Get(self):  # noqa: N802
        return self._value

    def Set(self, value) -> None:  # noqa: N802
        self.set_calls.append(value)
        self._value = value

    def GetConnections(self):  # noqa: N802
        return list(self._connections)


class FakePrim:
    def __init__(self, path: str, attrs: list[FakeAttr]):
        self._path = path
        self._attrs = {a.GetName(): a for a in attrs}

    def GetPath(self) -> str:  # noqa: N802
        return self._path

    def GetAttribute(self, name: str):  # noqa: N802
        return self._attrs.get(name)  # missing -> falsy (mirrors invalid Usd.Attribute)

    def GetAttributes(self):  # noqa: N802
        return list(self._attrs.values())


class FakeStage:
    def __init__(self, prims: list[FakePrim]):
        self._prims = {p.GetPath(): p for p in prims}

    def Traverse(self):  # noqa: N802
        return list(self._prims.values())

    def GetPrimAtPath(self, path):  # noqa: N802
        return self._prims.get(str(path))  # missing -> falsy (invalid prim)


def _lidar_graph(front_enabled=False, back_enabled=False) -> FakeStage:
    """Measured shape: publish <- compute <- render-product (per 2D lidar) +
    an unrelated exec source upstream (must be walked over harmlessly)."""
    g = "/World/Robot/graph"
    return FakeStage(
        [
            FakePrim(f"{g}/tick", [FakeAttr("node:type", "omni.graph.action.OnPlaybackTick")]),
            # front chain (names deliberately non-probe-literal)
            FakePrim(
                f"{g}/rp_a",
                [
                    FakeAttr("node:type", RENDER_PRODUCT_TYPE),
                    FakeAttr("inputs:enabled", front_enabled),
                    FakeAttr("inputs:execIn", connections=[f"{g}/tick"]),
                ],
            ),
            FakePrim(
                f"{g}/scan_a",
                [
                    FakeAttr("node:type", "isaacsim.sensors.rtx.IsaacComputeRTXLidarFlatScan"),
                    FakeAttr("inputs:renderProductPath", connections=[f"{g}/rp_a"]),
                ],
            ),
            FakePrim(
                f"{g}/pub_a",
                [
                    FakeAttr("node:type", "isaacsim.ros2.bridge.ROS2PublishLaserScan"),
                    FakeAttr("inputs:topicName", "/front_2d_lidar/scan"),
                    FakeAttr("inputs:execIn", connections=[f"{g}/scan_a"]),
                ],
            ),
            # back chain
            FakePrim(
                f"{g}/rp_b",
                [
                    FakeAttr("node:type", RENDER_PRODUCT_TYPE),
                    FakeAttr("inputs:enabled", back_enabled),
                ],
            ),
            FakePrim(
                f"{g}/pub_b",
                [
                    FakeAttr("node:type", "isaacsim.ros2.bridge.ROS2PublishLaserScan"),
                    FakeAttr("inputs:topicName", "/back_2d_lidar/scan"),
                    FakeAttr("inputs:renderProductPath", connections=[f"{g}/rp_b"]),
                ],
            ),
            # an always-on 3D lidar publisher with NO render-product gate upstream
            FakePrim(
                f"{g}/pub_3d",
                [
                    FakeAttr("node:type", "isaacsim.ros2.bridge.ROS2PublishPointCloud"),
                    FakeAttr("inputs:topicName", "/front_3d_lidar/lidar_points"),
                ],
            ),
        ]
    )


def test_enables_only_declared_topics_upstream_render_product():
    stage = _lidar_graph()
    enabled, unmatched = enable_sensor_render_products(stage, ["/front_2d_lidar/scan"])
    assert enabled == ["/World/Robot/graph/rp_a"]
    assert unmatched == []
    assert stage.GetPrimAtPath("/World/Robot/graph/rp_a").GetAttribute("inputs:enabled").Get()
    # undeclared back lidar stays untouched (declared-set semantics)
    assert not stage.GetPrimAtPath("/World/Robot/graph/rp_b").GetAttribute("inputs:enabled").Get()


def test_enables_every_declared_gated_topic():
    stage = _lidar_graph()
    enabled, unmatched = enable_sensor_render_products(
        stage,
        ["/front_3d_lidar/lidar_points", "/front_2d_lidar/scan", "/back_2d_lidar/scan"],
    )
    # 3D lidar has no gate upstream -> nothing to enable for it, and that is fine.
    assert enabled == ["/World/Robot/graph/rp_a", "/World/Robot/graph/rp_b"]
    assert unmatched == []


def test_idempotent_second_call_is_noop():
    stage = _lidar_graph()
    topics = ["/front_2d_lidar/scan", "/back_2d_lidar/scan"]
    first, _ = enable_sensor_render_products(stage, topics)
    assert len(first) == 2  # positive control: the first pass DID flip something
    second, _ = enable_sensor_render_products(stage, topics)
    assert second == []  # already enabled -> no Set, no report
    rp_a = stage.GetPrimAtPath("/World/Robot/graph/rp_a").GetAttribute("inputs:enabled")
    assert rp_a.set_calls == [True]  # exactly one in-memory write ever


def test_already_enabled_render_product_is_untouched():
    stage = _lidar_graph(front_enabled=True)
    enabled, _ = enable_sensor_render_products(stage, ["/front_2d_lidar/scan"])
    assert enabled == []
    attr = stage.GetPrimAtPath("/World/Robot/graph/rp_a").GetAttribute("inputs:enabled")
    assert attr.set_calls == []  # no write at all — asset state preserved


def test_declared_topic_without_publish_node_is_reported():
    # The original FU-17 bug class: declared in sensors[], no publisher in the
    # scene. The walk cannot fix that — it must surface it, not swallow it.
    enabled, unmatched = enable_sensor_render_products(_lidar_graph(), ["/no/such/topic"])
    assert enabled == []
    assert unmatched == ["/no/such/topic"]


def test_empty_declaration_is_noop():
    assert enable_sensor_render_products(_lidar_graph(), []) == ([], [])
