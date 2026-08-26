"""CPU tests for the SUT realign step (``cv_infra.runner.realign``) — no ROS needed.

Two surfaces, both of which fail SILENTLY in production if they are wrong:

* ``initialpose_fields`` — the AMCL seed's arithmetic (planar yaw -> quaternion,
  SIM-time stamp split, nav2's covariance literals). A wrong stamp or a wrong
  quaternion does not raise; it just re-seeds AMCL somewhere else and the next
  sample wanders.
* ``SutRealigner.realign`` — the OBSERVATION. Its dict is the batch summary's
  only evidence that this step happened (NEG-6 counts it n/n), so the keys are
  pinned, "not attempted" is kept distinct from "nobody listened", and every wait
  is proven bounded (a blackbox that never answers must not hang the carrier).

The ROS message/service TYPES are injected as fakes — the production default is
the bundled-Jazzy import, which is what the workstation exercises.
"""

import math
from types import SimpleNamespace

import pytest

from cv_infra.runner import realign as realign_mod
from cv_infra.runner.realign import (
    COSTMAP_CLEAR_SERVICES,
    INITIALPOSE_COVARIANCE,
    INITIALPOSE_TOPIC,
    REALIGN_OBSERVATION_KEYS,
    REALIGN_PUBLISH_COUNT,
    SutRealigner,
    apply_initialpose_fields,
    initialpose_fields,
)

POSE = {"x": -6.0, "y": -1.5, "yaw": math.pi / 2}


# --------------------------------------------------------------------------- #
# Fakes (shape-compatible stand-ins for geometry_msgs / nav2_msgs / rclpy).
# --------------------------------------------------------------------------- #
def _pose_msg_type():
    """A ``PoseWithCovarianceStamped``-SHAPED object (attribute tree + 36 floats)."""
    return SimpleNamespace(
        header=SimpleNamespace(frame_id="", stamp=SimpleNamespace(sec=0, nanosec=0)),
        pose=SimpleNamespace(
            pose=SimpleNamespace(
                position=SimpleNamespace(x=0.0, y=0.0, z=0.0),
                orientation=SimpleNamespace(x=0.0, y=0.0, z=0.0, w=1.0),
            ),
            covariance=[0.0] * 36,
        ),
    )


class _FakeClearSrv:
    class Request:  # noqa: D106 - the srv type's nested request, as ROS generates it
        pass


class _FakeFuture:
    def __init__(self, done_after: int = 0) -> None:
        self._left = done_after

    def done(self) -> bool:
        if self._left <= 0:
            return True
        self._left -= 1
        return False


class _FakePub:
    def __init__(self, subscribers) -> None:
        # int = constant; list = one answer per poll (then the last one sticks).
        self._subscribers = subscribers
        self.published: list = []

    def get_subscription_count(self) -> int:
        if isinstance(self._subscribers, int):
            return self._subscribers
        return self._subscribers.pop(0) if len(self._subscribers) > 1 else self._subscribers[0]

    def publish(self, msg) -> None:
        self.published.append(msg)


class _FakeClient:
    def __init__(self, ready=True, done_after: int = 0) -> None:
        self._ready = ready
        self._done_after = done_after
        self.calls: list = []

    def service_is_ready(self) -> bool:
        if isinstance(self._ready, bool):
            return self._ready
        return self._ready.pop(0) if len(self._ready) > 1 else self._ready[0]

    def call_async(self, request):
        self.calls.append(request)
        return _FakeFuture(self._done_after)


class _FakeNode:
    def __init__(self, pub=None, clients=None) -> None:
        self.pub = _FakePub(1) if pub is None else pub
        self.clients = {} if clients is None else clients
        self.created_publishers: list = []
        self.created_clients: list = []

    def create_publisher(self, msg_type, topic, depth):
        self.created_publishers.append((msg_type, topic, depth))
        return self.pub

    def create_client(self, srv_type, service):
        self.created_clients.append((srv_type, service))
        return self.clients.setdefault(service, _FakeClient())


def _realigner(node, steps: list) -> SutRealigner:
    return SutRealigner(
        node,
        lambda: steps.append(1),
        pose_msg_type=_pose_msg_type,
        clear_srv_type=_FakeClearSrv,
    )


# --------------------------------------------------------------------------- #
# initialpose_fields — the arithmetic that fails silently.
# --------------------------------------------------------------------------- #
def test_initialpose_fields_uses_the_planar_yaw_quaternion():
    fields = initialpose_fields({"x": 1.25, "y": -2.5, "yaw": math.pi / 2}, 0.0)
    assert fields["position_x"] == 1.25 and fields["position_y"] == -2.5
    # +Z only, the same convention the sim's own initial_pose transform writes.
    assert fields["orientation_z"] == pytest.approx(math.sin(math.pi / 4))
    assert fields["orientation_w"] == pytest.approx(math.cos(math.pi / 4))


def test_initialpose_fields_stamps_sim_time_not_wall_time():
    """D-F: the SUT runs on use_sim_time, so a wall stamp lands decades in its
    future and TF drops the pose. The split must be exact seconds + remainder."""
    fields = initialpose_fields(POSE, 12.25)
    assert fields["stamp_sec"] == 12
    assert fields["stamp_nanosec"] == pytest.approx(250_000_000, abs=1)
    assert fields["frame_id"] == "map"


def test_initialpose_fields_carries_nav2s_own_covariance():
    fields = initialpose_fields(POSE, 3.0)
    assert fields["covariance"] == INITIALPOSE_COVARIANCE
    assert sorted(fields["covariance"]) == [0, 7, 35]  # x, y, yaw on the 6x6 diagonal


def test_initialpose_fields_hands_out_a_copy_of_the_covariance():
    """A caller writing into the returned dict must not edit the module constant."""
    fields = initialpose_fields(POSE, 0.0)
    fields["covariance"][0] = 99.0
    assert INITIALPOSE_COVARIANCE[0] == 0.25


def test_apply_initialpose_fields_writes_every_field_onto_the_message():
    msg = apply_initialpose_fields(_pose_msg_type(), initialpose_fields(POSE, 7.5))
    assert msg.header.frame_id == "map" and msg.header.stamp.sec == 7
    assert msg.pose.pose.position.x == -6.0 and msg.pose.pose.position.y == -1.5
    assert msg.pose.pose.orientation.z == pytest.approx(math.sin(math.pi / 4))
    assert msg.pose.covariance[0] == 0.25
    assert msg.pose.covariance[7] == 0.25
    assert msg.pose.covariance[35] == pytest.approx(0.0685389, abs=1e-6)
    assert msg.pose.covariance[1] == 0.0  # off-diagonal untouched


# --------------------------------------------------------------------------- #
# realign — the observation dict (the batch summary's evidence).
# --------------------------------------------------------------------------- #
def test_realign_publishes_the_burst_and_clears_both_costmaps():
    steps: list = []
    node = _FakeNode(pub=_FakePub(subscribers=2))
    observed = _realigner(node, steps).realign(POSE, 4.0)

    assert observed["initialpose_subscribers"] == 2
    assert observed["initialpose_published"] == REALIGN_PUBLISH_COUNT
    assert len(node.pub.published) == REALIGN_PUBLISH_COUNT
    assert observed["costmaps_cleared"] == list(COSTMAP_CLEAR_SERVICES)
    assert observed["missing"] == []
    # Every wait PUMPS the sim: the burst alone owes one step per message (the
    # sim is the /clock source, so a wait that does not step stalls the SUT).
    assert len(steps) >= REALIGN_PUBLISH_COUNT
    assert node.created_publishers == [(_pose_msg_type, INITIALPOSE_TOPIC, 10)]


def test_realign_always_returns_the_same_keys():
    """Pinned BOTH ways: a key that appears only when it is interesting is a key
    nobody can count, and NEG-6 counts this dict n/n."""
    node = _FakeNode()
    observed = _realigner(node, []).realign(POSE, 1.0)
    assert tuple(observed) == REALIGN_OBSERVATION_KEYS
    without_pose = _realigner(_FakeNode(), []).realign(None, 1.0)
    assert tuple(without_pose) == REALIGN_OBSERVATION_KEYS


def test_realign_without_a_declared_pose_does_not_invent_one():
    """No initial_pose = the sim restored the asset's own placement; re-seeding
    AMCL with a pose we do not know would be an invention. Costmaps still clear
    (stale occupancy is stale either way)."""
    node = _FakeNode()
    observed = _realigner(node, []).realign(None, 9.0)
    assert observed["initialpose_subscribers"] is None  # "not attempted"
    assert observed["initialpose_published"] == 0
    assert node.created_publishers == []
    assert observed["costmaps_cleared"] == list(COSTMAP_CLEAR_SERVICES)


def test_realign_reports_zero_subscribers_instead_of_claiming_success(monkeypatch):
    """0 = "attempted, nobody was listening" — a DIFFERENT observation from None.

    This is the G-26 case the publisher-reuse design exists for: publishing into
    a topic no one has discovered reads exactly like a successful realign unless
    the count is reported.
    """
    monkeypatch.setattr(realign_mod, "DISCOVERY_TIMEOUT_S", 0.0)
    steps: list = []
    observed = _realigner(_FakeNode(pub=_FakePub(subscribers=0)), steps).realign(POSE, 1.0)
    assert observed["initialpose_subscribers"] == 0
    assert observed["initialpose_published"] == REALIGN_PUBLISH_COUNT  # honest: it did publish


def test_realign_discovery_wait_is_bounded_and_pumps_the_sim(monkeypatch):
    """The wait must END. With the bound at 0 the loop cannot run at all, which is
    how a hung blackbox becomes a reported observation instead of a hung carrier."""
    monkeypatch.setattr(realign_mod, "DISCOVERY_TIMEOUT_S", 0.0)
    steps: list = []
    _realigner(_FakeNode(pub=_FakePub(subscribers=0)), steps).realign(POSE, 1.0)
    assert len(steps) == REALIGN_PUBLISH_COUNT  # burst only: the discovery loop never spun


def test_realign_waits_for_the_subscription_before_publishing():
    steps: list = []
    pub = _FakePub(subscribers=[0, 0, 0, 1])  # discovered on the 4th poll
    observed = _realigner(_FakeNode(pub=pub), steps).realign(POSE, 1.0)
    assert observed["initialpose_subscribers"] == 1
    assert len(steps) == 3 + REALIGN_PUBLISH_COUNT  # 3 discovery steps + the burst


def test_realign_records_a_service_that_never_becomes_ready(monkeypatch):
    monkeypatch.setattr(realign_mod, "SERVICE_READY_TIMEOUT_S", 0.0)
    clients = {COSTMAP_CLEAR_SERVICES[0]: _FakeClient(ready=False)}
    node = _FakeNode(clients=clients)
    observed = _realigner(node, []).realign(POSE, 1.0)
    assert observed["missing"] == [COSTMAP_CLEAR_SERVICES[0]]
    assert observed["costmaps_cleared"] == [COSTMAP_CLEAR_SERVICES[1]]
    assert clients[COSTMAP_CLEAR_SERVICES[0]].calls == []  # never called


def test_realign_records_a_service_call_that_never_answers(monkeypatch):
    monkeypatch.setattr(realign_mod, "SERVICE_CALL_TIMEOUT_S", 0.0)
    clients = {s: _FakeClient(done_after=10) for s in COSTMAP_CLEAR_SERVICES}
    observed = _realigner(_FakeNode(clients=clients), []).realign(None, 1.0)
    assert observed["costmaps_cleared"] == []
    assert observed["missing"] == list(COSTMAP_CLEAR_SERVICES)


def test_realign_without_a_wired_node_reports_it_instead_of_crashing():
    observed = _realigner(None, []).realign(POSE, 1.0)
    assert observed["missing"] == ["rclpy node (adapter not wired)"]
    assert observed["initialpose_published"] == 0


def test_publisher_and_clients_outlive_an_iteration():
    """They are created ONCE per carrier on purpose: a publisher created and used
    in the same instant has not been discovered yet (G-26), so re-creating them
    every sample would make every realign a no-op that reads as done."""
    node = _FakeNode()
    realigner = _realigner(node, [])
    realigner.realign(POSE, 1.0)
    realigner.realign(POSE, 2.0)
    assert len(node.created_publishers) == 1
    assert [service for _type, service in node.created_clients] == list(COSTMAP_CLEAR_SERVICES)


def test_costmap_services_are_nav2s_published_interfaces():
    """Blackbox stance (REQ-EXEC-005): the realign touches nothing INSIDE the SUT."""
    assert COSTMAP_CLEAR_SERVICES == (
        "/global_costmap/clear_entirely_global_costmap",
        "/local_costmap/clear_entirely_local_costmap",
    )
    assert INITIALPOSE_TOPIC == "/initialpose"
