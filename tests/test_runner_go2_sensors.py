"""CPU tests for the runner-published go2 sensor streams — no ROS, no GPU.

This module is the SUT-facing surface of a composed world: if it is wrong, the
symptom is never an exception. A swapped quaternion order puts the robot in
rviz facing the wrong way; a world-frame twist makes nav2's controller think the
robot strafes; a scan whose ``angle_min`` disagrees with its array puts every
obstacle at the wrong bearing; a ``/tf_static`` that is not transient-local
simply never reaches a SUT node that started late. So the wire VALUES are
asserted here, term by term, against the two vendor seams replaced by fakes:

* ``StageSensors`` — a recording duck with the same accessors the Isaac one has;
* ``RosTypes`` — message classes that auto-create their nested fields exactly
  like a real ROS message does (``msg.pose.pose.position.x = 1.0``).

What is NOT provable here, and is the workstation's: that the RTX lidar and the
camera annotators actually produce data through this code path (probe A2/B).
"""

import math
import os
import subprocess
import sys

import pytest

from cv_infra.contract.adapter_schema import Ros2AdapterConfig, SensorInput
from cv_infra.runner.go2_policy import quat_apply_inverse
from cv_infra.runner.go2_sensors import (
    CAMERA_MOUNT_XYZ,
    CAMERA_OPTICAL_QUAT_WXYZ,
    CAMERA_RATE_HZ,
    LIDAR_MOUNT_XYZ,
    ODOM_RATE_HZ,
    SCAN_RATE_HZ,
    SENSOR_INVENTORY_LOG_MARKER,
    STREAM_CAMERA_INFO,
    STREAM_DEPTH,
    STREAM_RGB,
    STREAM_SCAN,
    TF_STATIC_TOPIC,
    TF_TOPIC,
    FirstDataGate,
    Go2SensorSuite,
    RateGate,
    RosTypes,
    StageSensors,
    camera_info_fields,
    camera_intrinsics,
    classify_sensor,
    inventory_lines,
    odom_fields,
    plan_sensor_streams,
    quat_wxyz_to_xyzw,
    scan_fields,
    scene_needs_runner_sensors,
    sensor_suite_for,
    sim_time_stamp,
    static_transforms,
    topic_inventory,
)
from cv_infra.runner.sim_runtime import SCENE_ASSETS

UPRIGHT = (1.0, 0.0, 0.0, 0.0)
#: yaw +90 deg — the robot faces world +y.
YAW_90 = (0.7071067811865476, 0.0, 0.0, 0.7071067811865476)

GO2_SENSORS = [
    SensorInput(topic="/camera/image_raw", type="sensor_msgs/msg/Image"),
    SensorInput(topic="/camera/depth/image_raw", type="sensor_msgs/msg/Image"),
    SensorInput(topic="/camera/camera_info", type="sensor_msgs/msg/CameraInfo"),
    SensorInput(topic="/scan", type="sensor_msgs/msg/LaserScan"),
]


def _config(**kwargs) -> Ros2AdapterConfig:
    defaults = {"odom_topics": ["/odom"], "sensors": list(GO2_SENSORS)}
    defaults.update(kwargs)
    return Ros2AdapterConfig(**defaults)


# --------------------------------------------------------------------------- #
# Fakes.
# --------------------------------------------------------------------------- #
class _Msg:
    """A ROS-message-shaped attribute bag: nested fields spring into existence.

    Real generated messages already carry their sub-messages, so
    ``msg.pose.pose.position.x = 1.0`` just works; the fake has to do the same or
    the code under test would need a shape it does not have in production.
    """

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        child = _Msg()
        object.__setattr__(self, name, child)
        return child


def _msg_types(names):
    return {name: type(name, (_Msg,), {}) for name in names}


class _FakePublisher:
    def __init__(self, msg_type, topic, qos):
        self.msg_type = msg_type
        self.topic = topic
        self.qos = qos
        self.messages = []

    def publish(self, msg):
        self.messages.append(msg)


class _FakeNode:
    def __init__(self):
        self.publishers = []

    def create_publisher(self, msg_type, topic, qos):
        publisher = _FakePublisher(msg_type, topic, qos)
        self.publishers.append(publisher)
        return publisher

    def by_topic(self, topic):
        return next(p for p in self.publishers if p.topic == topic)


class _FakeArray:
    """Minimal stand-in for the numpy frames the annotators return."""

    def __init__(self, shape, payload=b"\x01\x02"):
        self.shape = shape
        self.size = shape[0] * shape[1] * (shape[2] if len(shape) > 2 else 1)
        self._payload = payload

    def tobytes(self):
        return self._payload


class _FakeStage(StageSensors):
    """Recording stand-in for the Isaac stage sensors (same accessors)."""

    def __init__(self, *, rgb=None, depth=None, scan=None):
        super().__init__(body=None)
        self.position = (1.0, 2.0, 0.3)
        self.quat = UPRIGHT
        self.linear = (0.0, 0.0, 0.0)
        self.angular = (0.0, 0.0, 0.0)
        self.initialized = 0
        self._rgb = rgb if rgb is not None else _FakeArray((480, 640, 3), b"rgb")
        self._depth = depth if depth is not None else _FakeArray((480, 640), b"depth")
        self._scan = scan if scan is not None else _flat_scan()

    def pose(self):
        return self.position, self.quat

    def velocity(self):
        return self.linear, self.angular

    def initialize(self):
        self.initialized += 1

    def calibration(self):
        return (640, 480, 1.2, 2.0955, 1.5716249465942382)

    def rgb(self):
        return self._rgb

    def depth(self):
        return self._depth

    def scan(self):
        return self._scan


def _flat_scan(depths=(1.0, -1.0, 3.0, 4.0)):
    """One flat-scan annotator frame, in the shape the real one returns."""
    return {
        "linearDepthData": list(depths),
        "azimuthRange": [-180.0, 179.8875],
        "horizontalResolution": 0.1125,
        "depthRange": [0.05, 30.0],
        "rotationRate": 10.0,
    }


def _qos(depth=5, transient_local=False):
    return {"depth": depth, "transient_local": transient_local}


def _ros_types():
    classes = _msg_types(
        ["Clock", "TFMessage", "TransformStamped", "Odometry", "Image", "CameraInfo", "LaserScan"]
    )
    return RosTypes(qos=_qos, **classes)


def _suite(config=None, stage=None, chassis="/World/Go2/base"):
    stage = stage if stage is not None else _FakeStage()
    suite = Go2SensorSuite(
        config if config is not None else _config(),
        chassis,
        stage_factory=lambda _path, _streams: stage,
        ros_types_factory=_ros_types,
    )
    return suite, stage


def _attached(config=None, stage=None):
    suite, stage = _suite(config, stage)
    node = _FakeNode()
    suite.bind(object())
    lines = suite.attach(node)
    return suite, stage, node, lines


# --------------------------------------------------------------------------- #
# Declaration -> streams.
# --------------------------------------------------------------------------- #
def test_each_declared_type_selects_its_stream_and_depth_is_told_by_its_namespace():
    """The two Image streams cannot be told apart by message type, so the ROS
    image_pipeline namespace convention decides — and only as a whole PATH
    SEGMENT, so a topic like /depth_camera/image_raw is not silently depth."""
    assert classify_sensor("/camera/image_raw", "sensor_msgs/msg/Image") == STREAM_RGB
    assert classify_sensor("/camera/depth/image_raw", "sensor_msgs/msg/Image") == STREAM_DEPTH
    assert classify_sensor("/camera/camera_info", "sensor_msgs/msg/CameraInfo") == (
        STREAM_CAMERA_INFO
    )
    assert classify_sensor("/scan", "sensor_msgs/msg/LaserScan") == STREAM_SCAN
    assert classify_sensor("/depthcam/image_raw", "sensor_msgs/msg/Image") == STREAM_RGB
    assert classify_sensor("/front_3d_lidar/points", "sensor_msgs/msg/PointCloud2") is None


def test_the_go2_declaration_plans_four_streams_with_the_default_frames():
    streams, unsupported = plan_sensor_streams(GO2_SENSORS)
    assert unsupported == []
    assert {k: s.topic for k, s in streams.items()} == {
        STREAM_RGB: "/camera/image_raw",
        STREAM_DEPTH: "/camera/depth/image_raw",
        STREAM_CAMERA_INFO: "/camera/camera_info",
        STREAM_SCAN: "/scan",
    }
    assert streams[STREAM_RGB].frame == "go2_camera"
    assert streams[STREAM_SCAN].frame == "go2_lidar"


def test_one_declared_camera_frame_is_shared_by_the_whole_camera_family():
    """A depth image unprojected with a camera_info stamped in a DIFFERENT frame
    lands wherever that other frame is — so the family takes one frame id."""
    sensors = [
        SensorInput(topic="/cam/rgb", type="sensor_msgs/msg/Image", frame="eye"),
        SensorInput(topic="/cam/depth/img", type="sensor_msgs/msg/Image"),
        SensorInput(topic="/cam/info", type="sensor_msgs/msg/CameraInfo"),
    ]
    streams, _ = plan_sensor_streams(sensors)
    assert {s.frame for s in streams.values()} == {"eye"}


def test_a_declared_scan_frame_is_honoured_and_does_not_leak_into_the_camera():
    streams, _ = plan_sensor_streams(
        [
            SensorInput(topic="/scan", type="sensor_msgs/msg/LaserScan", frame="laser"),
            SensorInput(topic="/cam/rgb", type="sensor_msgs/msg/Image"),
        ]
    )
    assert streams[STREAM_SCAN].frame == "laser"
    assert streams[STREAM_RGB].frame == "go2_camera"


def test_an_unsupported_declaration_is_reported_in_its_DECLARED_spelling():
    """FU-17's bug class in its runner-published form: declared, but nothing
    publishes it. Silence here is a scenario that believes it has a lidar."""
    streams, unsupported = plan_sensor_streams(
        [SensorInput(topic="/front_3d_lidar/lidar_points", type="sensor_msgs/msg/PointCloud2")]
    )
    assert streams == {}
    assert unsupported == ["/front_3d_lidar/lidar_points"]


def test_two_topics_asking_for_the_same_stream_is_a_loud_rejection():
    with pytest.raises(ValueError, match="same 'camera_rgb' stream"):
        plan_sensor_streams(
            [
                SensorInput(topic="/cam/rgb", type="sensor_msgs/msg/Image"),
                SensorInput(topic="/other/rgb", type="sensor_msgs/msg/Image"),
            ]
        )


def test_only_a_scene_that_composes_its_own_robot_needs_runner_sensors():
    """The registry already knows: a row with ``robot_usd`` is a world we
    ASSEMBLED (no vendor graph, not even /clock); carter's row is a pre-wired
    sample whose graphs we must not duplicate."""
    assert scene_needs_runner_sensors(SCENE_ASSETS["go2_warehouse"]) is True
    assert scene_needs_runner_sensors(SCENE_ASSETS["nova_carter_warehouse"]) is False


def test_sensor_suite_for_returns_none_on_a_prewired_scene():
    assert sensor_suite_for(SCENE_ASSETS["nova_carter_warehouse"], _config(), "/x") is None
    suite = sensor_suite_for(SCENE_ASSETS["go2_warehouse"], _config(), "/World/Go2/base")
    assert isinstance(suite, Go2SensorSuite)
    assert set(suite.streams) == {STREAM_RGB, STREAM_DEPTH, STREAM_CAMERA_INFO, STREAM_SCAN}


# --------------------------------------------------------------------------- #
# Rate gating (sim time).
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("rate", [10.0, 30.0])
def test_a_gate_fires_exactly_its_rate_over_one_second_of_sim_time(rate):
    """dt = 0.005 s is the go2 step (D-4). The count over one second IS the rate:
    a step-counting decimation would silently change it with fixed_dt."""
    gate = RateGate(rate)
    fired = sum(1 for i in range(200) if gate.due(i * 0.005))
    assert fired == int(rate)


def test_the_first_observation_is_always_due_and_a_stall_does_not_burst():
    gate = RateGate(10.0)
    assert gate.due(0.0) is True
    assert gate.due(0.05) is False
    # a 5 s stall: the next observation fires ONCE, not 50 catch-up times
    assert gate.due(5.0) is True
    assert gate.due(5.05) is False
    assert gate.due(5.1) is True


def test_a_nonpositive_rate_is_rejected():
    with pytest.raises(ValueError, match="rate_hz must be > 0"):
        RateGate(0.0)


def test_the_data_gate_announces_first_data_once_and_warns_when_it_never_comes():
    gate = FirstDataGate("scan", patience_s=1.0)
    assert gate.observe(0, 0.0) is None  # inside the patience window
    assert gate.observe(0, 0.5) is None
    warning = gate.observe(0, 1.5)
    assert warning is not None and "EMPTY frames" in warning
    assert gate.observe(0, 2.0) is None  # warned once, not every step
    first = gate.observe(3200, 2.5)
    assert first is not None and "first data at sim_time=2.500s (3200 sample(s))" in first
    assert gate.observe(3200, 2.6) is None
    assert "first data at 2.500s" in gate.summary()


def test_a_stream_that_never_produced_data_says_so_at_teardown():
    assert "NEVER produced data" in FirstDataGate("camera_rgb").summary()


# --------------------------------------------------------------------------- #
# Field math.
# --------------------------------------------------------------------------- #
def test_sim_time_becomes_a_ros_stamp_without_naming_a_future_moment():
    assert sim_time_stamp(0.0) == (0, 0)
    assert sim_time_stamp(1.5) == (1, 500_000_000)
    assert sim_time_stamp(0.005) == (0, 5_000_000)
    # the rounded nanoseconds carry into the next second instead of overflowing
    assert sim_time_stamp(2.9999999999) == (3, 0)


def test_the_isaac_scalar_first_quaternion_is_reordered_for_the_ros_wire():
    assert quat_wxyz_to_xyzw((0.1, 0.2, 0.3, 0.4)) == (0.2, 0.3, 0.4, 0.1)


def test_the_camera_static_transform_is_the_ros_OPTICAL_rotation():
    """The images are stamped in this frame, so a pixel unprojected with K lands
    in it directly: base +x (forward) must become optical +z, base +z (up) must
    become optical -y. A 90 deg error here is invisible until a detection is
    projected onto the wrong wall."""
    forward = quat_apply_inverse(CAMERA_OPTICAL_QUAT_WXYZ, (1.0, 0.0, 0.0))
    up = quat_apply_inverse(CAMERA_OPTICAL_QUAT_WXYZ, (0.0, 0.0, 1.0))
    left = quat_apply_inverse(CAMERA_OPTICAL_QUAT_WXYZ, (0.0, 1.0, 0.0))
    assert forward == pytest.approx((0.0, 0.0, 1.0), abs=1e-12)
    assert up == pytest.approx((0.0, -1.0, 0.0), abs=1e-12)
    assert left == pytest.approx((-1.0, 0.0, 0.0), abs=1e-12)


def test_static_transforms_cover_exactly_the_declared_sensor_frames():
    streams, _ = plan_sensor_streams(GO2_SENSORS)
    transforms = static_transforms("base_link", streams)
    assert [(t.parent, t.child) for t in transforms] == [
        ("base_link", "go2_camera"),
        ("base_link", "go2_lidar"),
    ]
    assert transforms[0].translation == CAMERA_MOUNT_XYZ
    assert transforms[1].translation == LIDAR_MOUNT_XYZ
    # MEASURED (probe A2): the RTX lidar's azimuth zero is the prim's +X and
    # angles grow counter-clockwise (a post off the robot's RIGHT came back at
    # -49 deg), which IS the ROS LaserScan convention -> no rotation.
    assert transforms[1].rotation_xyzw == (0.0, 0.0, 0.0, 1.0)
    assert static_transforms("base_link", {}) == []


def test_a_camera_only_declaration_publishes_no_lidar_transform():
    streams, _ = plan_sensor_streams([SensorInput(topic="/i", type="sensor_msgs/msg/Image")])
    assert [t.child for t in static_transforms("base_link", streams)] == ["go2_camera"]


def test_odometry_pose_is_world_but_the_twist_is_rotated_into_the_body():
    """nav_msgs/Odometry splits its frames: pose in header.frame_id (odom),
    twist in child_frame_id (base_link). A world-frame twist reads to nav2's
    controller as a robot strafing whenever it is not facing +x."""
    fields = odom_fields((1.0, 2.0, 0.3), YAW_90, (1.0, 0.0, 0.0), (0.0, 0.0, 0.5))
    assert fields.position == (1.0, 2.0, 0.3)
    half_root_two = 0.7071067811865476
    assert fields.orientation_xyzw == pytest.approx((0.0, 0.0, half_root_two, half_root_two))
    # facing +y while moving along world +x = moving to its own RIGHT (body -y)
    assert fields.linear == pytest.approx((0.0, -1.0, 0.0), abs=1e-12)
    assert fields.angular == pytest.approx((0.0, 0.0, 0.5), abs=1e-12)


def test_camera_intrinsics_reproduce_the_measured_vendor_matrix():
    """Cross-checked on GPU (probe A2): Isaac's own get_intrinsics_matrix returned
    fx = 366.4996 for exactly these attributes, and the FOV measured 82.25 deg."""
    # the apertures are the float32 values READ BACK off the prim, verbatim
    fx, fy, cx, cy = camera_intrinsics(640, 480, 1.2, 2.0954999923706055, 1.5716249465942382)
    assert fx == pytest.approx(366.49964342456235, rel=1e-12)
    assert fy == pytest.approx(fx, rel=1e-6)  # square pixels
    assert (cx, cy) == (320.0, 240.0)
    assert 2 * math.degrees(math.atan(2.0955 / (2 * 1.2))) == pytest.approx(82.25, abs=0.01)
    assert camera_intrinsics(640, 480, 1.2, 2.0955, 1.5716)[0] == pytest.approx(fx, abs=1e-4)


def test_camera_intrinsics_reject_a_zero_or_negative_optic():
    with pytest.raises(ValueError, match="positive focal length"):
        camera_intrinsics(640, 480, 0.0, 2.0955, 1.5716)
    with pytest.raises(ValueError, match="positive focal length"):
        camera_intrinsics(640, 480, 1.2, 2.0955, 0.0)


def test_camera_info_is_a_rectified_monocular_pinhole():
    info = camera_info_fields(640, 480, 1.2, 2.0954999923706055, 1.5716249465942382)
    fx = info.k[0]
    assert (info.width, info.height) == (640, 480)
    assert info.distortion_model == "plumb_bob"
    assert info.d == (0.0,) * 5
    assert info.k[2] == 320.0 and info.k[5] == 240.0 and info.k[8] == 1.0
    assert info.r == (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0)
    # P = K with a zero translation column (REP-104): same fx/cx, 4th col zero
    assert info.p[0] == fx and info.p[2] == 320.0 and info.p[3] == 0.0
    assert info.p[7] == 0.0 and info.p[10] == 1.0 and info.p[11] == 0.0


def test_scan_no_return_becomes_infinity_not_a_zero_range_obstacle():
    """MEASURED: the annotator writes -1.0 for a ray that hit nothing. Passing
    that through would put an obstacle 1 m BEHIND every empty bearing."""
    fields = scan_fields([1.0, -1.0, 3.0], [-180.0, 179.8875], 0.1125, [0.05, 30.0], 0.1)
    assert fields.ranges == (1.0, math.inf, 3.0)
    assert fields.range_min == 0.05
    assert fields.range_max == 30.0


def test_scan_angles_are_radians_and_angle_max_is_derived_from_the_array():
    """Consumers compute ray i's bearing as angle_min + i * increment; deriving
    angle_max from the same arithmetic is what keeps the two from contradicting."""
    fields = scan_fields([1.0] * 3200, [-180.0, 179.8875], 0.1125, [0.05, 30.0], 0.1)
    assert fields.angle_min == pytest.approx(-math.pi)
    assert fields.angle_increment == pytest.approx(math.radians(0.1125))
    assert fields.angle_max == pytest.approx(math.radians(-180.0 + 3199 * 0.1125))
    # ...and that derivation agrees with what the sensor reported (179.8875 deg)
    assert math.degrees(fields.angle_max) == pytest.approx(179.8875, abs=1e-6)
    assert fields.scan_time == 0.1
    assert fields.time_increment == pytest.approx(0.1 / 3200)


def test_an_empty_scan_is_a_loud_refusal_to_publish_a_beamless_laserscan():
    with pytest.raises(ValueError, match="0 beams"):
        scan_fields([], [-180.0, 180.0], 0.1125, [0.05, 30.0], 0.1)


# --------------------------------------------------------------------------- #
# Inventory.
# --------------------------------------------------------------------------- #
def test_the_inventory_names_every_topic_with_its_rate_frame_and_gating():
    config = _config(odom_topics=["/odom", "/chassis/odom"])
    streams, _ = plan_sensor_streams(GO2_SENSORS)
    rows = topic_inventory(config, streams)
    by_topic = {row.topic: row for row in rows}
    assert set(by_topic) == {
        "/clock",
        TF_TOPIC,
        TF_STATIC_TOPIC,
        "/odom",
        "/chassis/odom",
        "/camera/image_raw",
        "/camera/depth/image_raw",
        "/camera/camera_info",
        "/scan",
    }
    assert by_topic["/clock"].gating == "always"
    assert by_topic["/odom"].rate_hz == f"{ODOM_RATE_HZ:g}"
    assert by_topic["/scan"].rate_hz == f"{SCAN_RATE_HZ:g}"
    assert by_topic["/camera/image_raw"].rate_hz == f"{CAMERA_RATE_HZ:g}"
    assert by_topic["/camera/image_raw"].gating == "declared"
    assert by_topic[TF_STATIC_TOPIC].frame == "base_link->go2_camera,base_link->go2_lidar"
    assert by_topic[TF_TOPIC].frame == "odom->base_link"


def test_the_inventory_lines_carry_the_grep_marker_and_one_line_per_topic():
    rows = topic_inventory(_config(), plan_sensor_streams(GO2_SENSORS)[0])
    lines = inventory_lines(rows)
    assert lines[0] == f"[cv-runner] {SENSOR_INVENTORY_LOG_MARKER}{len(rows)}"
    assert len(lines) == len(rows) + 1
    assert any(
        "/scan  sensor_msgs/msg/LaserScan  10 Hz  frame=go2_lidar  (declared)" in line
        for line in lines
    )


def test_a_scene_without_declared_sensors_still_publishes_the_always_on_set():
    rows = topic_inventory(_config(sensors=[]), {})
    assert [row.topic for row in rows] == ["/clock", TF_TOPIC, "/odom"]


# --------------------------------------------------------------------------- #
# The suite: lifecycle + publishing.
# --------------------------------------------------------------------------- #
def test_attach_creates_one_publisher_per_topic_with_the_right_type():
    suite, stage, node, _lines = _attached()
    topics = {p.topic: p for p in node.publishers}
    assert set(topics) == {
        "/clock",
        TF_TOPIC,
        TF_STATIC_TOPIC,
        "/odom",
        "/camera/image_raw",
        "/camera/depth/image_raw",
        "/camera/camera_info",
        "/scan",
    }
    assert topics["/camera/image_raw"].msg_type is topics["/camera/depth/image_raw"].msg_type
    assert topics["/camera/camera_info"].msg_type.__name__ == "CameraInfo"
    assert topics["/scan"].msg_type.__name__ == "LaserScan"
    assert stage.initialized == 1


def test_tf_static_is_transient_local_so_a_late_sut_node_still_gets_it():
    """A volatile /tf_static reaches only the nodes that were already listening —
    and it is published exactly once, at attach."""
    _suite_obj, _stage, node, _lines = _attached()
    static = node.by_topic(TF_STATIC_TOPIC)
    assert static.qos == {"depth": 1, "transient_local": True}
    assert node.by_topic("/scan").qos == {"depth": 5, "transient_local": False}
    assert len(static.messages) == 1
    children = [t.child_frame_id for t in static.messages[0].transforms]
    assert children == ["go2_camera", "go2_lidar"]
    camera = static.messages[0].transforms[0]
    assert camera.header.frame_id == "base_link"
    assert (camera.transform.translation.x, camera.transform.translation.z) == (
        CAMERA_MOUNT_XYZ[0],
        CAMERA_MOUNT_XYZ[2],
    )


def test_one_second_of_stepping_publishes_every_stream_at_its_declared_rate():
    """The whole point of the module in one assertion: /clock every step (it is
    the SUT's time base, not a sensor), odom at 30, camera and scan at 10."""
    suite, _stage, node, _lines = _attached()
    for i in range(200):  # 1 s at the go2 fixed_dt
        suite.publish(i * 0.005)
    published = {p.topic: len(p.messages) for p in node.publishers}
    assert published["/clock"] == 200
    assert published["/odom"] == 30
    assert published[TF_TOPIC] == 30
    assert published["/camera/image_raw"] == 10
    assert published["/camera/depth/image_raw"] == 10
    assert published["/camera/camera_info"] == 10
    assert published["/scan"] == 10


def test_every_message_is_stamped_with_SIM_time():
    suite, _stage, node, _lines = _attached()
    suite.publish(12.25)
    clock = node.by_topic("/clock").messages[0]
    assert (clock.clock.sec, clock.clock.nanosec) == (12, 250_000_000)
    odom = node.by_topic("/odom").messages[0]
    assert (odom.header.stamp.sec, odom.header.stamp.nanosec) == (12, 250_000_000)
    assert node.by_topic("/scan").messages[0].header.stamp.sec == 12


def test_the_odom_message_and_the_tf_transform_carry_the_same_gt_pose():
    """They are two views of one fact; if they drift the SUT's costmap and its
    controller disagree about where the robot is."""
    stage = _FakeStage()
    stage.position = (3.0, -1.0, 0.28)
    stage.quat = YAW_90
    stage.linear = (1.0, 0.0, 0.0)
    suite, _stage, node, _lines = _attached(stage=stage)
    suite.publish(1.0)
    odom = node.by_topic("/odom").messages[0]
    transform = node.by_topic(TF_TOPIC).messages[0].transforms[0]
    assert odom.header.frame_id == "odom" and odom.child_frame_id == "base_link"
    assert transform.header.frame_id == "odom" and transform.child_frame_id == "base_link"
    assert (odom.pose.pose.position.x, odom.pose.pose.position.y) == (3.0, -1.0)
    assert (transform.transform.translation.x, transform.transform.translation.y) == (3.0, -1.0)
    assert odom.pose.pose.orientation.w == pytest.approx(0.7071067811865476)
    assert transform.transform.rotation.w == pytest.approx(0.7071067811865476)
    # twist is in the BODY frame (facing +y, driving world +x = its own right)
    assert odom.twist.twist.linear.y == pytest.approx(-1.0, abs=1e-12)


def test_the_odom_stream_fans_out_to_every_declared_odom_topic():
    """Measured on carter (G-63): nav2 subscribes to odom under two names and
    both must flow. Here we are the source, so both get published directly."""
    suite, _stage, node, _lines = _attached(config=_config(odom_topics=["/odom", "/chassis/odom"]))
    suite.publish(0.0)
    assert len(node.by_topic("/odom").messages) == 1
    assert len(node.by_topic("/chassis/odom").messages) == 1


def test_the_rgb_and_depth_images_carry_their_encoding_step_and_bytes():
    suite, _stage, node, _lines = _attached()
    suite.publish(0.0)
    rgb = node.by_topic("/camera/image_raw").messages[0]
    depth = node.by_topic("/camera/depth/image_raw").messages[0]
    assert (rgb.height, rgb.width, rgb.encoding, rgb.step) == (480, 640, "rgb8", 1920)
    assert rgb.data == b"rgb" and rgb.is_bigendian == 0
    assert (depth.encoding, depth.step) == ("32FC1", 2560)
    assert depth.data == b"depth"
    assert rgb.header.frame_id == depth.header.frame_id == "go2_camera"


def test_camera_info_is_built_once_from_the_calibration_read_off_the_prim():
    """G-26: the published K is what the camera IS, not what we asked it to be —
    it comes from the read-back attributes, so a set that did not stick shows up
    in camera_info instead of hiding."""
    suite, _stage, node, _lines = _attached()
    suite.publish(0.0)
    info = node.by_topic("/camera/camera_info").messages[0]
    assert (info.width, info.height) == (640, 480)
    assert info.k[0] == pytest.approx(366.49964342456235)
    assert info.distortion_model == "plumb_bob"
    assert info.header.frame_id == "go2_camera"


def test_the_scan_message_is_the_measured_flat_scan_in_ros_units():
    suite, _stage, node, _lines = _attached()
    suite.publish(0.0)
    scan = node.by_topic("/scan").messages[0]
    assert scan.header.frame_id == "go2_lidar"
    assert scan.ranges == [1.0, math.inf, 3.0, 4.0]
    assert scan.angle_min == pytest.approx(-math.pi)
    assert scan.range_max == 30.0
    assert scan.scan_time == pytest.approx(0.1)


def test_an_empty_scan_frame_publishes_nothing_and_warns_once(capsys):
    """C0 probe §6-3: a mis-driven RTX lidar returns an EMPTY array, never an
    exception. Publishing a 0-beam LaserScan would hand nav2 a valid-looking
    message that says 'there is nothing anywhere'."""
    stage = _FakeStage(scan={"linearDepthData": []})
    suite, _stage, node, _lines = _attached(stage=stage)
    for i in range(1000):  # 5 s of sim time at dt 0.005
        suite.publish(i * 0.005)
    assert node.by_topic("/scan").messages == []
    out = capsys.readouterr().out
    assert out.count("has produced EMPTY frames") == 1
    assert "NEVER produced data" in "\n".join(suite.detach())


def test_a_renderer_that_has_not_warmed_up_yet_is_skipped_then_announced(capsys):
    stage = _FakeStage()
    stage._rgb = None  # the renderer has produced nothing yet
    suite, _stage, node, _lines = _attached(stage=stage)
    suite.publish(0.0)
    assert node.by_topic("/camera/image_raw").messages == []
    stage._rgb = _FakeArray((480, 640, 3), b"rgb")
    suite.publish(0.1)
    assert len(node.by_topic("/camera/image_raw").messages) == 1
    assert "camera_rgb: first data at sim_time=0.100s" in capsys.readouterr().out


def test_publishing_before_attach_or_after_detach_is_a_no_op():
    suite, stage = _suite()
    suite.bind(object())
    suite.publish(0.0)  # no publishers yet: nothing to do, and no crash
    node = _FakeNode()
    suite.attach(node)
    suite.publish(0.0)
    assert suite.published["clock"] == 1
    suite.detach()
    suite.publish(1.0)
    assert suite.published["clock"] == 1


def test_attach_without_bind_names_the_missing_step():
    suite, _stage = _suite()
    with pytest.raises(RuntimeError, match="bind\\(world\\) must run"):
        suite.attach(_FakeNode())


def test_binding_without_a_chassis_path_is_refused_not_guessed():
    """R7: the mount point comes from the scenario's measured criteria params —
    a hardcoded scene path is exactly what this platform must not ship."""
    suite, _stage = _suite(chassis="")
    with pytest.raises(RuntimeError, match="chassis_path"):
        suite.bind(object())


def test_the_step_hook_reads_the_worlds_own_sim_clock():
    """``publish_from_world`` is what SimRuntime.on_step calls, so the sim clock
    (not a wall clock, D-F) is the only time this module ever stamps."""

    class _World:
        current_time = 4.5

    suite, stage = _suite()
    node = _FakeNode()
    suite.bind(_World())
    on_step = []
    suite.attach(node, on_step)
    assert on_step == [suite.publish_from_world]
    on_step[0]()
    assert node.by_topic("/clock").messages[0].clock.sec == 4


def test_attach_returns_the_inventory_and_flags_an_unpublishable_declaration():
    sensors = [*GO2_SENSORS, SensorInput(topic="/points", type="sensor_msgs/msg/PointCloud2")]
    _suite_obj, _stage, _node, lines = _attached(config=_config(sensors=sensors))
    assert lines[0].startswith(f"[cv-runner] {SENSOR_INVENTORY_LOG_MARKER}")
    assert any("cannot publish" in line and "/points" in line for line in lines)


def test_a_config_without_odom_topics_is_flagged_loudly():
    _suite_obj, _stage, _node, lines = _attached(config=_config(odom_topics=[]))
    assert any("declares NO odom_topics" in line for line in lines)


def test_a_declaration_free_run_still_publishes_clock_tf_and_odom():
    suite, _stage, node, _lines = _attached(config=_config(sensors=[]))
    suite.publish(0.0)
    assert {p.topic for p in node.publishers} == {"/clock", TF_TOPIC, TF_STATIC_TOPIC, "/odom"}
    assert node.by_topic(TF_STATIC_TOPIC).messages == []  # nothing static to say
    assert len(node.by_topic("/clock").messages) == 1
    assert suite.detach() == []


# --------------------------------------------------------------------------- #
# Import surface.
# --------------------------------------------------------------------------- #
def test_importing_the_sensor_module_pulls_no_ros_isaac_or_numpy():
    """It is imported by ``runner.main`` on every job, including carter's, and by
    the host-side test suite — neither may need a ROS install to do it."""
    code = (
        "import sys; import cv_infra.runner.go2_sensors\n"
        "roots = {'torch', 'isaacsim', 'omni', 'carb', 'pxr', 'rclpy', 'cv2', 'numpy'}\n"
        "print(sorted(m for m in sys.modules if m.split('.')[0] in roots))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        env={k: v for k, v in os.environ.items() if k != "PYTHONPATH"},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "[]"


def test_the_stage_accessors_hand_the_pure_math_plain_floats():
    """Isaac returns numpy arrays from ``get_world_pose``/``get_*_velocity``; the
    pure field math below is stdlib-only (numpy is not on the CPU test plane), so
    the conversion is part of the seam and not of every caller."""

    class _Body:
        def get_world_pose(self):
            return [1, 2, 3], [1, 0, 0, 0]

        def get_linear_velocity(self):
            return [0.5, 0, 0]

        def get_angular_velocity(self):
            return [0, 0, 0.25]

    stage = StageSensors(body=_Body())
    assert stage.pose() == ((1.0, 2.0, 3.0), (1.0, 0.0, 0.0, 0.0))
    assert stage.velocity() == ((0.5, 0.0, 0.0), (0.0, 0.0, 0.25))


# --------------------------------------------------------------------------- #
# The wiring block in ``runner.main`` (D-2) — pre-boot, so it is CPU-reachable.
# --------------------------------------------------------------------------- #
def _request(scene: str, sensors: list[dict] | None = None):
    from cv_infra.contract.schema import VerificationRequest

    return VerificationRequest.model_validate(
        {
            "scenario": {
                "scene": scene,
                "robot": "go2",
                "goal": {"x": 1.0, "y": 0.0, "yaw": 0.0},
                "seed": 3,
                "timeout_s": 60.0,
            },
            "sut": {"image_ref": "ghcr.io/example/app@sha256:" + "a" * 64},
            "interface": {
                "type": "ros2",
                "adapter_config": {"odom_topics": ["/odom"], "sensors": sensors or []},
            },
            "acceptance_criteria": [{"oracle": "reached_goal"}],
        }
    )


def test_main_builds_the_suite_for_a_composed_scene_and_skips_a_prewired_one():
    from cv_infra.runner.main import build_sensor_suite

    criteria = {"chassis_path": "/World/Go2/base"}
    sensors = [{"topic": "/scan", "type": "sensor_msgs/msg/LaserScan"}]
    assert build_sensor_suite(_request("nova_carter_warehouse", sensors), criteria) is None
    suite = build_sensor_suite(_request("go2_warehouse", sensors), criteria)
    assert isinstance(suite, Go2SensorSuite)
    assert suite.chassis_path == "/World/Go2/base"


def test_a_sensor_declaration_the_runner_cannot_serve_is_rejected_pre_boot():
    """0 GPU seconds and exit 2, like the obstacle-asset check next to it: a
    duplicate stream discovered mid-boot would be a platform failure for what is
    plainly a bad document."""
    from cv_infra.runner.main import BadJobSpec, build_sensor_suite

    duplicated = [
        {"topic": "/cam/a", "type": "sensor_msgs/msg/Image"},
        {"topic": "/cam/b", "type": "sensor_msgs/msg/Image"},
    ]
    with pytest.raises(BadJobSpec, match="interface.sensors"):
        build_sensor_suite(_request("go2_warehouse", duplicated), {"chassis_path": "/x"})


def test_an_unknown_scene_name_is_now_rejected_pre_boot_too():
    from cv_infra.runner.main import BadJobSpec, build_sensor_suite

    with pytest.raises(BadJobSpec, match="unknown scenario.scene"):
        build_sensor_suite(_request("no_such_scene"), {"chassis_path": "/x"})


def test_the_runner_prints_every_line_a_collaborator_reports(capsys):
    from cv_infra.runner.main import _emit

    _emit(["one", "two"])
    assert capsys.readouterr().out == "one\ntwo\n"
