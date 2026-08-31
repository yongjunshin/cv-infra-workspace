"""Runner-published SUT-facing sensor streams for a COMPOSED scene (M2, D-2).

The carter sample scene ships its own ROS 2 OmniGraphs — clock, TF, odom and the
lidar publishers come WITH the asset, and the adapter only supplements them
(``adapter/ros2.py``). A go2 world has none of that: it is a robot-free
warehouse plus a referenced robot (D-1), and the go2 USD carries no camera, no
lidar and no graph at all (C0 probe A1: zero Camera/OmniLidar prims). So for
that world the runner IS the sensor stack, and this module is it.

What it publishes, and on whose say-so:

* **always** (a SUT cannot drive without them): ``/clock`` — the sim-time source
  the whole D-F budget rests on — plus ``/tf`` (``odom``->``base_link``),
  ``/tf_static`` (``base_link``->sensor frames) and ``/odom``. Every topic NAME
  comes from ``adapter_config`` (``clock_topic`` / ``odom_topics`` / ``frames``),
  never from a literal here (R7);
* **only when DECLARED** in ``interface.sensors[]``: the camera streams
  (rgb / depth / camera_info) and ``/scan``. This mirrors FU-17's semantics
  (``sim_runtime.enable_sensor_render_products``) for a scene that has no graph
  to enable: a declared topic this runner cannot serve is reported LOUDLY
  instead of being dropped, which is the FU-17 bug class ("declared but
  publisher-less") in its runner-published form.

Ground truth, not odometry (LOCKED §7): ``/odom`` and ``/tf`` are built from the
chassis body's ``get_world_pose()``. That makes the SUT's odom drift-free, which
is the same deal the carter sample gives (its odom is sim-published too), and it
is why ``odom`` coincides with the world frame — the carter occupancy map is
therefore valid for the SUT's ``map`` frame as well (C1 §4).

Isaac and rclpy are DEFERRED behind two injected seams (``stage_factory`` /
``ros_types``), so every decision this module makes — which streams exist, when
each one is due, what goes in each message field — is a pure function or a
duck-typed call and is CPU-unit-tested without a GPU or a ROS install. The two
seams themselves are the only vendor-coupled code.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from cv_infra.contract.adapter_schema import Ros2AdapterConfig, SensorInput
from cv_infra.runner.go2_policy import quat_apply_inverse

# --------------------------------------------------------------------------- #
# Frames and mounts — MEASURED, then written (C3 probe A2, 2026-09-01).
# --------------------------------------------------------------------------- #
#: TF is the one place a topic name is NOT taken from adapter_config: ``/tf`` and
#: ``/tf_static`` are hard-coded inside tf2 itself (every listener subscribes to
#: exactly these), so a configurable name here would only be a way to be wrong.
TF_TOPIC = "/tf"
TF_STATIC_TOPIC = "/tf_static"

#: Sensor frame ids used when the scenario declares no ``frame`` for a stream.
DEFAULT_CAMERA_FRAME = "go2_camera"
DEFAULT_LIDAR_FRAME = "go2_lidar"

#: Camera mount, base_link -> camera, metres. MEASURED: the base subtree (trunk
#: + head) spans x [-0.128, +0.332] / y +-0.097 / z [-0.097, +0.089] around the
#: base origin, so 0.28 m forward sits at the head, and frames rendered from
#: (0.28, 0, 0.12) and (0.34, 0, 0.12) both showed ZERO self-occlusion. 0.28 is
#: adopted (the more conservative of the two measured positions: it keeps 5 cm of
#: margin to the head tip, so a future asset revision does not put the lens
#: outside the robot).
CAMERA_MOUNT_XYZ = (0.28, 0.0, 0.12)
#: base_link -> camera OPTICAL frame (x right, y down, z forward — REP-103), the
#: standard ``(x, y, z, w) = (-0.5, 0.5, -0.5, 0.5)`` written w-first. The images
#: are stamped with THIS frame, so a pixel unprojected with ``camera_info`` lands
#: in it directly and tf2 alone takes the detection to ``map`` — no second
#: "camera_link" frame and no optical-rotation trap in the consumer.
CAMERA_OPTICAL_QUAT_WXYZ = (0.5, -0.5, 0.5, -0.5)

#: Lidar mount, base_link -> lidar, metres. MEASURED (probe A2, 3200-beam scans
#: in the warehouse, stance held):
#:   z = -0.05 / 0.00 (inside the trunk) -> 299 / 0 valid returns of 3200
#:   z = +0.15                           -> 2723 valid, nearest return 1.256 m
#:   z = +0.20                           -> 2339 valid
#: i.e. the body DOES occlude the sensor (that is the manipulation check for
#: "no self-hits", G-107 (2)) and +0.15 clears it — 6 cm above the measured trunk
#: top — while keeping the scan plane low (~0.43 m above the floor when standing,
#: so a 0.877 m chair and a 1.73 m person are both in it).
LIDAR_MOUNT_XYZ = (0.0, 0.0, 0.15)

# --------------------------------------------------------------------------- #
# Camera optics — MEASURED (probe A2).
# --------------------------------------------------------------------------- #
CAMERA_RESOLUTION = (640, 480)
#: ``Camera.set_focal_length`` takes STAGE units, which are the USD attribute /10
#: (``USD_CAMERA_TENTHS_TO_STAGE_UNIT``). MEASURED trap: passing 12.0 here writes
#: ``focalLength = 120`` and yields a **9.98 deg** horizontal FOV — a frame with
#: nothing in it, which is C0 probe §6-10's symptom with a different cause. 1.2
#: writes the intended ``focalLength = 12`` against the stock 20.955 aperture and
#: measures 82.25 deg x 66.44 deg (fx = fy = 366.50 px at 640x480).
CAMERA_FOCAL_LENGTH_STAGE_UNITS = 1.2
#: MEASURED: the stock USD near clip is 1.0 m, which blacked out the bottom 23 %
#: of every frame (69,882 of 307,200 pixels, exactly the non-finite depth count)
#: and floored the depth image at 1.000 m. With 0.05 m the same view is 95 %
#: finite and reads 0.607 m at the nearest floor pixel.
CAMERA_CLIPPING_RANGE_M = (0.05, 100.0)
#: A pinhole render has no lens distortion; the ROS name for "all zeros" is
#: plumb_bob (consumers switch on this string).
CAMERA_DISTORTION_MODEL = "plumb_bob"
CAMERA_DISTORTION_COEFFS = (0.0, 0.0, 0.0, 0.0, 0.0)
#: rgb8 (3 bytes/px) + 32FC1 depth: the two encodings image_pipeline expects for
#: an RGB-D pair, and the two the annotators natively produce.
RGB_ENCODING = "rgb8"
DEPTH_ENCODING = "32FC1"

# --------------------------------------------------------------------------- #
# RTX lidar — config selection MEASURED (AR-10, probe A1: one boot, six configs,
# same mount, same warehouse).
# --------------------------------------------------------------------------- #
#  config             beams  hFOV      resolution  rate    depthRange
#  RPLIDAR_S2E        3200   360.00    0.1125 deg  10 Hz   [0.05, 30]   <- adopted
#  TIM781              811   359.67    0.4435 deg  15 Hz   [0.05, 25]
#  microScan3          537   359.49    0.6694 deg  33 Hz   [0.05, 40]
#  nanoScan3          1651   275.00    0.1666 deg  33 Hz   [0.05, 40]
#  picoScan150         276   358.996   1.3007 deg  15 Hz   [0.05, 75]
#  Example_Rotary_2D  1066   360.00    0.3375 deg  30 Hz   [1.00, 200]
# AR-10's bar was "min range <= 0.3 m, 360 deg, ~10 Hz". Example_Rotary_2D (the
# config C0 measured) fails it on min range alone — 1.0 m of blind ring around a
# 0.7 m robot. RPLIDAR_S2E is the only candidate that meets all three, and its
# 0.1125 deg resolution is the finest of the 360 deg set as well.
LIDAR_CONFIG = "RPLIDAR_S2E"
#: MEASURED: the flat-scan annotator marks a ray with no return as **-1.0**, not
#: 0.0 and not NaN. ROS says "out of range" is +inf, and nav2/AMCL read it that
#: way, so the mapping happens here rather than in every consumer.
LIDAR_NO_RETURN = -1.0

# --------------------------------------------------------------------------- #
# Publish rates (sim-time, D-F). All gating is on ``World.current_time``.
# --------------------------------------------------------------------------- #
#: ``/clock`` is published on EVERY step: it is not a sensor, it is the clock the
#: SUT's whole time base (and our own readiness barrier, G-19) runs on, so the
#: rate is the step rate by construction and nothing decimates it.
ODOM_RATE_HZ = 30.0  # /odom + /tf (odom->base_link)
CAMERA_RATE_HZ = 10.0  # rgb + depth + camera_info
SCAN_RATE_HZ = 10.0  # = the adopted lidar's own 10 Hz rotation rate

#: QoS depth for every publisher here. Everything is published RELIABLE, which is
#: the compatible-with-everything choice for a PUBLISHER (a reliable publisher
#: satisfies both reliable and best-effort subscribers, while a best-effort
#: publisher is invisible to a reliable subscriber — and nav2 mixes the two:
#: SensorDataQoS on scans, system defaults on odom). KEEP_LAST(5) bounds the
#: buffer so a slow subscriber drops old frames instead of blocking the sim.
QOS_DEPTH = 5
#: ``/tf_static`` MUST be transient-local: it is published once, and every SUT
#: node that starts later has to still receive it (that is the tf2 contract).
TF_STATIC_QOS_DEPTH = 1

#: Verbatim grep marker (G-26 prove-it-ran gate; pinned by a CPU test): the topic
#: inventory this runner actually created. A sensor stack that silently published
#: nothing and one that was never asked to publish read the same in a log.
SENSOR_INVENTORY_LOG_MARKER = "go2_sensors inventory="

# Stream keys — the internal names for the four declarable streams.
STREAM_RGB = "camera_rgb"
STREAM_DEPTH = "camera_depth"
STREAM_CAMERA_INFO = "camera_info"
STREAM_SCAN = "scan"

_IMAGE_TYPE = "sensor_msgs/msg/Image"
_CAMERA_INFO_TYPE = "sensor_msgs/msg/CameraInfo"
_LASER_SCAN_TYPE = "sensor_msgs/msg/LaserScan"
#: A declared Image topic is the DEPTH one when its name carries a ``depth`` path
#: segment (``/camera/depth/image_raw``) — the ROS image_pipeline convention. The
#: two Image streams cannot be told apart by message type, and this rule is the
#: one every ROS camera driver already follows; the boot inventory prints which
#: topic became which stream, so a mis-binding is visible immediately.
_DEPTH_SEGMENT = "depth"

_SUPPORTED_TYPES = {_IMAGE_TYPE, _CAMERA_INFO_TYPE, _LASER_SCAN_TYPE}


# --------------------------------------------------------------------------- #
# Declaration -> streams (pure).
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class SensorStream:
    """One declared stream this runner will publish: its topic and frame id."""

    key: str
    topic: str
    frame: str


def classify_sensor(topic: str, type_name: str) -> str | None:
    """Which stream a declared ``sensors[]`` entry asks for — None = unsupported."""
    if type_name == _CAMERA_INFO_TYPE:
        return STREAM_CAMERA_INFO
    if type_name == _LASER_SCAN_TYPE:
        return STREAM_SCAN
    if type_name == _IMAGE_TYPE:
        segments = [s for s in str(topic).split("/") if s]
        return STREAM_DEPTH if _DEPTH_SEGMENT in segments else STREAM_RGB
    return None


def plan_sensor_streams(
    sensors: Iterable[SensorInput],
) -> tuple[dict[str, SensorStream], list[str]]:
    """Declared sensors -> ``{stream key: SensorStream}`` + the unsupported ones.

    The second list is the FU-17 bug class in its runner-published form (declared
    but nothing publishes it) and the caller says it out loud. Two declarations
    of the SAME stream are a loud ValueError instead of a silent last-one-wins:
    the runner renders one camera and one lidar, so a second rgb topic is a
    scenario that believes it asked for something it did not get.

    Frames are resolved here too, and per FAMILY rather than per entry: the three
    camera streams must be stamped with ONE frame id or a consumer unprojecting
    depth with camera_info gets a transform that does not exist. The first
    declared ``frame`` in the family wins; an undeclared family takes the module
    default.
    """
    matched: dict[str, SensorInput] = {}
    unsupported: list[str] = []
    for sensor in sensors:
        key = classify_sensor(sensor.topic, sensor.type)
        if key is None:
            unsupported.append(sensor.topic)
            continue
        if key in matched:
            raise ValueError(
                f"interface.sensors[] declares {sensor.topic!r} and "
                f"{matched[key].topic!r} as the same {key!r} stream — this runner "
                "renders one camera and one lidar, so only one topic per stream can "
                "carry it (declare the other one only if the SUT remaps it)"
            )
        matched[key] = sensor
    camera_frame = _family_frame(matched, (STREAM_RGB, STREAM_DEPTH, STREAM_CAMERA_INFO))
    frames = {
        STREAM_RGB: camera_frame or DEFAULT_CAMERA_FRAME,
        STREAM_DEPTH: camera_frame or DEFAULT_CAMERA_FRAME,
        STREAM_CAMERA_INFO: camera_frame or DEFAULT_CAMERA_FRAME,
        STREAM_SCAN: _family_frame(matched, (STREAM_SCAN,)) or DEFAULT_LIDAR_FRAME,
    }
    streams = {
        key: SensorStream(key=key, topic=sensor.topic, frame=frames[key])
        for key, sensor in matched.items()
    }
    return streams, unsupported


def _family_frame(matched: dict[str, SensorInput], keys: tuple[str, ...]) -> str | None:
    """First declared ``frame`` among ``keys`` (declaration order), else None."""
    for key in keys:
        sensor = matched.get(key)
        if sensor is not None and sensor.frame:
            return sensor.frame
    return None


def scene_needs_runner_sensors(asset: object) -> bool:
    """True when the scene's ROS streams have to come from THIS module.

    The registry already records the difference (D-1): a row that declares
    ``robot_usd`` is a robot-free environment the runner ASSEMBLES, and an
    assembled world has no vendor OmniGraph publishing anything — not even
    ``/clock``. A row without it is a pre-wired sample (carter), whose graphs we
    reuse and must not duplicate. Inferring it from the registry rather than from
    a scene NAME is what keeps a second composed robot working with no edit here.
    """
    return bool(getattr(asset, "robot_usd", None))


# --------------------------------------------------------------------------- #
# Sim-time rate gate (pure).
# --------------------------------------------------------------------------- #
_EPS = 1e-9


class RateGate:
    """ "Is this stream due at sim-time t?" — sim-time keyed, dt-independent.

    Gating on the sim CLOCK rather than on a step count is what makes the
    published rates hold at any ``fixed_dt`` (go2 runs at 0.005, carter at 1/60)
    and across a render decimation that would change the step-to-time ratio.

    The next deadline advances by exactly one period, so a period that is a whole
    number of steps produces the exact rate with no drift; after a stall longer
    than one period the deadline is re-based on NOW instead of firing a burst of
    catch-up messages nobody can use (the sim is the only clock, so a "late"
    sensor message is not late — it is a message about a moment that has passed).
    """

    def __init__(self, rate_hz: float) -> None:
        if rate_hz <= 0:
            raise ValueError(f"rate_hz must be > 0, got {rate_hz!r}")
        self.period_s = 1.0 / float(rate_hz)
        self._next: float | None = None

    def due(self, sim_time_s: float) -> bool:
        if self._next is not None and sim_time_s + _EPS < self._next:
            return False
        base = sim_time_s if self._next is None else self._next
        self._next = base + self.period_s
        if self._next <= sim_time_s:
            self._next = sim_time_s + self.period_s
        return True


class FirstDataGate:
    """Loud, ONCE, when a stream starts producing — and when it never does.

    C0 probe §6-3's trap is that a mis-driven RTX lidar fails by returning an
    EMPTY array, not by raising: the topic exists, the rate looks right, and
    every range is missing. Probe A2 reproduced the same silence from a sensor
    buried inside the robot's own body (0 beams of 3200). So the beam count is
    gated explicitly: the first non-empty frame prints what it carries, and a
    stream that is still empty after ``patience_s`` of SIM TIME says so once.
    """

    def __init__(self, name: str, patience_s: float = 2.0) -> None:
        self.name = name
        self.patience_s = float(patience_s)
        self.first_data_at: float | None = None
        self._warned = False
        self._start: float | None = None

    def observe(self, count: int, sim_time_s: float) -> str | None:
        """Return the one line to print for this observation, or None."""
        if self._start is None:
            self._start = sim_time_s
        if count > 0:
            if self.first_data_at is not None:
                return None
            self.first_data_at = sim_time_s
            return (
                f"[cv-runner] go2_sensors {self.name}: first data at "
                f"sim_time={sim_time_s:.3f}s ({count} sample(s))"
            )
        if self._warned or sim_time_s - self._start < self.patience_s:
            return None
        self._warned = True
        return (
            f"[cv-runner] WARNING: go2_sensors {self.name} has produced EMPTY frames "
            f"for {sim_time_s - self._start:.1f}s of sim time — the sensor is "
            "attached but returns no samples (C0 probe §6-3: an RTX lidar fails "
            "this way, not by raising)"
        )

    def summary(self) -> str:
        """One line at teardown: did this stream ever carry anything?"""
        if self.first_data_at is None:
            return f"[cv-runner] WARNING: go2_sensors {self.name} NEVER produced data"
        return f"[cv-runner] go2_sensors {self.name}: first data at {self.first_data_at:.3f}s"


# --------------------------------------------------------------------------- #
# Message field math (pure) — the values, separate from the vendor message copy.
# --------------------------------------------------------------------------- #
def sim_time_stamp(sim_time_s: float) -> tuple[int, int]:
    """Sim seconds -> ``(sec, nanosec)`` for a ROS stamp (D-F: sim time only).

    Truncation, not rounding: a stamp must never name a moment the sim has not
    reached, and ``builtin_interfaces/Time`` has no signed nanosecond field.
    """
    sec = int(math.floor(sim_time_s))
    nanosec = int(round((sim_time_s - sec) * 1e9))
    if nanosec >= 1_000_000_000:  # rounding carried into the next second
        sec += 1
        nanosec -= 1_000_000_000
    return sec, nanosec


@dataclass(frozen=True)
class TransformFields:
    """One TF transform: parent, child, translation, rotation (x, y, z, w)."""

    parent: str
    child: str
    translation: tuple[float, float, float]
    rotation_xyzw: tuple[float, float, float, float]


def quat_wxyz_to_xyzw(quat_wxyz: Iterable[float]) -> tuple[float, float, float, float]:
    """Isaac's scalar-FIRST quaternion -> the ROS scalar-LAST wire order.

    One home for the reorder (G-25). Isaac (``get_world_pose``, ``PoseSample``)
    is w-first everywhere; every ROS message field is x, y, z, w. A swap here is
    silent — the robot simply faces the wrong way in rviz and AMCL diverges.
    """
    w, x, y, z = (float(v) for v in quat_wxyz)
    return (x, y, z, w)


def static_transforms(base_link: str, streams: dict[str, SensorStream]) -> list[TransformFields]:
    """``base_link`` -> the sensor frames THIS run actually publishes.

    Only declared streams get a transform: a static TF to a frame no message ever
    carries is clutter that a consumer will nonetheless wire against.
    """
    transforms: list[TransformFields] = []
    camera = streams.get(STREAM_RGB) or streams.get(STREAM_DEPTH) or streams.get(STREAM_CAMERA_INFO)
    if camera is not None:
        transforms.append(
            TransformFields(
                parent=base_link,
                child=camera.frame,
                translation=CAMERA_MOUNT_XYZ,
                rotation_xyzw=quat_wxyz_to_xyzw(CAMERA_OPTICAL_QUAT_WXYZ),
            )
        )
    scan = streams.get(STREAM_SCAN)
    if scan is not None:
        transforms.append(
            TransformFields(
                parent=base_link,
                child=scan.frame,
                translation=LIDAR_MOUNT_XYZ,
                # The RTX lidar prim is authored with no rotation and its azimuth
                # zero is the prim's +X with positive angles going counter-
                # clockwise — MEASURED with a post 1.5 m off the robot's RIGHT,
                # which came back at azimuth -49 deg. That is exactly the ROS
                # LaserScan convention, so the scan frame IS base_link's
                # orientation and this transform is a pure translation.
                rotation_xyzw=(0.0, 0.0, 0.0, 1.0),
            )
        )
    return transforms


@dataclass(frozen=True)
class OdomFields:
    """``nav_msgs/Odometry`` values: GT pose in ``odom``, twist in ``base_link``."""

    position: tuple[float, float, float]
    orientation_xyzw: tuple[float, float, float, float]
    linear: tuple[float, float, float]
    angular: tuple[float, float, float]


def odom_fields(
    position: Iterable[float],
    quat_wxyz: Iterable[float],
    linear_world: Iterable[float],
    angular_world: Iterable[float],
) -> OdomFields:
    """GT world pose/velocity -> the Odometry message's values.

    ``nav_msgs/Odometry`` splits its frames: the pose is in ``header.frame_id``
    (odom = the world) and the TWIST is in ``child_frame_id`` (base_link), so the
    world-frame velocities Isaac reports have to be rotated into the body. nav2's
    controller reads that twist as the robot's own forward/strafe/yaw rate, and a
    world-frame twist would read as a robot driving sideways whenever it is not
    facing +X. The rotation is ``go2_policy.quat_apply_inverse`` — the same one
    the policy's observation uses, so the two cannot disagree.
    """
    quat = tuple(float(v) for v in quat_wxyz)
    return OdomFields(
        position=tuple(float(v) for v in position),
        orientation_xyzw=quat_wxyz_to_xyzw(quat),
        linear=quat_apply_inverse(quat, tuple(float(v) for v in linear_world)),
        angular=quat_apply_inverse(quat, tuple(float(v) for v in angular_world)),
    )


@dataclass(frozen=True)
class CameraInfoFields:
    """``sensor_msgs/CameraInfo`` values for a distortion-free pinhole render."""

    width: int
    height: int
    distortion_model: str
    d: tuple[float, ...]
    k: tuple[float, ...]
    r: tuple[float, ...]
    p: tuple[float, ...]


def camera_intrinsics(
    width: int,
    height: int,
    focal_length: float,
    horizontal_aperture: float,
    vertical_aperture: float,
) -> tuple[float, float, float, float]:
    """USD camera attributes -> ``(fx, fy, cx, cy)`` in pixels.

    This is the pinhole model Isaac's own ``Camera.get_intrinsics_matrix`` uses
    (``fx = width * focal / horizontal_aperture``, principal point at the image
    centre), re-derived here for two reasons: it is the value that goes on the
    wire, and it must be computed from the attributes READ BACK off the prim
    rather than from the constants we intended to set (G-26). The ratio is
    unit-free, so it does not matter whether the caller passes stage units or the
    raw USD tenths — as long as it passes BOTH in the same one.

    Cross-checked on GPU (probe A2, 640x480, focal 1.2, aperture 2.0955 stage
    units): this returns fx = 366.49964, the vendor matrix says 366.49963
    (float32), cx/cy = 320/240 — same numbers, and 82.25 deg x 66.44 deg of FOV.
    """
    if focal_length <= 0 or horizontal_aperture <= 0 or vertical_aperture <= 0:
        raise ValueError(
            "camera intrinsics need positive focal length and apertures, got "
            f"focal={focal_length!r} h_aperture={horizontal_aperture!r} "
            f"v_aperture={vertical_aperture!r}"
        )
    fx = width * focal_length / horizontal_aperture
    fy = height * focal_length / vertical_aperture
    return fx, fy, width * 0.5, height * 0.5


def camera_info_fields(
    width: int,
    height: int,
    focal_length: float,
    horizontal_aperture: float,
    vertical_aperture: float,
) -> CameraInfoFields:
    """The full CameraInfo payload (K/P/R/D) for the rendered pinhole camera."""
    fx, fy, cx, cy = camera_intrinsics(
        width, height, focal_length, horizontal_aperture, vertical_aperture
    )
    return CameraInfoFields(
        width=int(width),
        height=int(height),
        distortion_model=CAMERA_DISTORTION_MODEL,
        d=CAMERA_DISTORTION_COEFFS,
        k=(fx, 0.0, cx, 0.0, fy, cy, 0.0, 0.0, 1.0),
        # A monocular camera is its own rectified frame: R = identity and P is K
        # with a zero translation column (REP-104).
        r=(1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0),
        p=(fx, 0.0, cx, 0.0, 0.0, fy, cy, 0.0, 0.0, 0.0, 1.0, 0.0),
    )


@dataclass(frozen=True)
class ScanFields:
    """``sensor_msgs/LaserScan`` values built from ONE flat-scan frame."""

    angle_min: float
    angle_max: float
    angle_increment: float
    time_increment: float
    scan_time: float
    range_min: float
    range_max: float
    ranges: tuple[float, ...]


def scan_fields(
    depths: Iterable[float],
    azimuth_range_deg: Iterable[float],
    horizontal_resolution_deg: float,
    depth_range_m: Iterable[float],
    scan_time_s: float,
) -> ScanFields:
    """Flat-scan annotator output -> LaserScan values (angles in radians).

    Everything except ``scan_time`` comes from the annotator's own metadata, not
    from the config we asked for: the sensor is the authority on what it just
    produced, and a hard-coded beam count is how a config change becomes a silent
    frame/ranges mismatch.

    ``angle_max`` is DERIVED (``angle_min + (n-1) * increment``) rather than
    copied from the reported azimuth end. They agree on the measured sensor
    (-180 + 3199 * 0.1125 = 179.8875 = the reported value) — but LaserScan
    consumers compute the bearing of ray i as ``angle_min + i * increment`` and
    range-check it against ``angle_max``, so the derived value is the one that
    cannot contradict the array.
    """
    ranges = tuple(math.inf if d <= LIDAR_NO_RETURN else float(d) for d in depths)
    if not ranges:
        raise ValueError("flat scan carried 0 beams — nothing to publish")
    azimuth = [float(v) for v in azimuth_range_deg]
    depth_range = [float(v) for v in depth_range_m]
    increment = math.radians(float(horizontal_resolution_deg))
    angle_min = math.radians(azimuth[0])
    return ScanFields(
        angle_min=angle_min,
        angle_max=angle_min + increment * (len(ranges) - 1),
        angle_increment=increment,
        time_increment=float(scan_time_s) / len(ranges),
        scan_time=float(scan_time_s),
        range_min=depth_range[0],
        range_max=depth_range[1],
        ranges=ranges,
    )


# --------------------------------------------------------------------------- #
# Topic inventory (pure) — the U1/consumer-facing statement of what exists.
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class InventoryRow:
    """One published topic: what it is, how often, in which frame, and why."""

    topic: str
    type: str
    rate: str  # "every step" | "latched" | "<n> Hz" — self-describing, see below
    frame: str
    gating: str  # "always" | "declared"


def topic_inventory(
    config: Ros2AdapterConfig, streams: dict[str, SensorStream], step_rate_hz: float | None = None
) -> list[InventoryRow]:
    """Every topic this suite publishes, in one table (log + report + U1 input)."""
    # The rate column is a STRING because three of its values are not numbers:
    # /clock ticks once per sim step (whatever the scenario's fixed_dt makes
    # that) and /tf_static is latched, not periodic. Rendering those as a number
    # would invite a consumer to divide by it.
    clock_rate = "every step" if step_rate_hz is None else f"{step_rate_hz:g} Hz"
    rows = [
        InventoryRow(config.clock_topic, "rosgraph_msgs/msg/Clock", clock_rate, "-", "always"),
        InventoryRow(
            TF_TOPIC,
            "tf2_msgs/msg/TFMessage",
            f"{ODOM_RATE_HZ:g} Hz",
            f"{config.frames.odom}->{config.frames.base_link}",
            "always",
        ),
    ]
    static = static_transforms(config.frames.base_link, streams)
    if static:
        rows.append(
            InventoryRow(
                TF_STATIC_TOPIC,
                "tf2_msgs/msg/TFMessage",
                "latched",
                ",".join(f"{t.parent}->{t.child}" for t in static),
                "declared",
            )
        )
    rows.extend(
        InventoryRow(
            topic,
            "nav_msgs/msg/Odometry",
            f"{ODOM_RATE_HZ:g} Hz",
            f"{config.frames.odom}->{config.frames.base_link}",
            "always",
        )
        for topic in config.odom_topics
    )
    types = {
        STREAM_RGB: (_IMAGE_TYPE, CAMERA_RATE_HZ),
        STREAM_DEPTH: (_IMAGE_TYPE, CAMERA_RATE_HZ),
        STREAM_CAMERA_INFO: (_CAMERA_INFO_TYPE, CAMERA_RATE_HZ),
        STREAM_SCAN: (_LASER_SCAN_TYPE, SCAN_RATE_HZ),
    }
    rows.extend(
        InventoryRow(stream.topic, types[key][0], f"{types[key][1]:g} Hz", stream.frame, "declared")
        for key, stream in sorted(streams.items())
    )
    return rows


def inventory_lines(rows: list[InventoryRow]) -> list[str]:
    """The inventory as printable lines — one header, one per topic."""
    header = f"[cv-runner] {SENSOR_INVENTORY_LOG_MARKER}{len(rows)}"
    return [header] + [
        f"[cv-runner]   {row.topic}  {row.type}  {row.rate}  frame={row.frame}" f"  ({row.gating})"
        for row in rows
    ]


# --------------------------------------------------------------------------- #
# Vendor seam 1: the Isaac stage sensors (Camera + RTX lidar + GT pose source).
# --------------------------------------------------------------------------- #
@dataclass
class StageSensors:
    """The live Isaac objects, behind duck-typed accessors the suite calls.

    Everything above this line is pure; everything inside the methods below is
    ``isaacsim`` and therefore GPU-only. Keeping the vendor surface in ONE class
    is what lets the whole suite be exercised on CPU with a recording fake, the
    same way ``Go2PolicyLoop`` takes an articulation.
    """

    body: object  # SingleRigidPrim over the chassis (GT pose/velocity)
    camera: object | None = None  # isaacsim.sensors.camera.Camera
    lidar: object | None = None  # isaacsim.sensors.rtx.LidarRtx

    def pose(self) -> tuple[tuple[float, ...], tuple[float, ...]]:
        position, quat = self.body.get_world_pose()
        return tuple(float(v) for v in position), tuple(float(v) for v in quat)

    def velocity(self) -> tuple[tuple[float, ...], tuple[float, ...]]:
        return (
            tuple(float(v) for v in self.body.get_linear_velocity()),
            tuple(float(v) for v in self.body.get_angular_velocity()),
        )

    def initialize(self) -> None:  # pragma: no cover - GPU path (probe A2 measured)
        """Post-reset init. MEASURED: without ``initialize()`` the RTX lidar's
        acquisition callback is never registered and the annotator returns an
        EMPTY array forever — which is C0 probe §6-3's "empty scan" trap. The
        camera's clipping range and focal length are set HERE, after
        ``initialize()`` has created the render product."""
        if self.camera is not None:
            self.camera.initialize()
            self.camera.set_focal_length(CAMERA_FOCAL_LENGTH_STAGE_UNITS)
            self.camera.set_clipping_range(*CAMERA_CLIPPING_RANGE_M)
            self.camera.add_distance_to_image_plane_to_frame()
        if self.lidar is not None:
            self.lidar.initialize()

    def calibration(self) -> tuple[int, int, float, float, float]:  # pragma: no cover - GPU path
        """``(width, height, focal, h_aperture, v_aperture)`` READ BACK off the prim."""
        width, height = self.camera.get_resolution()
        return (
            int(width),
            int(height),
            float(self.camera.get_focal_length()),
            float(self.camera.get_horizontal_aperture()),
            float(self.camera.get_vertical_aperture()),
        )

    def rgb(self):  # pragma: no cover - GPU path
        """HxWx3 uint8, or None until the renderer has produced a frame."""
        import numpy as np  # noqa: PLC0415 (legal post-SimulationApp, D-C)

        data = self.camera.get_rgba()
        if data is None or getattr(data, "size", 0) == 0:
            return None
        frame = np.asarray(data)
        if frame.ndim != 3 or frame.shape[2] < 3:
            return None
        return np.ascontiguousarray(frame[:, :, :3]).astype(np.uint8)

    def depth(self):  # pragma: no cover - GPU path
        """HxW float32 distance-to-image-plane, or None before the first frame."""
        import numpy as np  # noqa: PLC0415

        data = self.camera.get_depth()
        if data is None or getattr(data, "size", 0) == 0:
            return None
        return np.ascontiguousarray(np.asarray(data)).astype(np.float32)

    def scan(self) -> dict | None:  # pragma: no cover - GPU path
        """The flat-scan annotator's last frame, or None when it holds nothing."""
        return self.lidar.get_current_frame().get("IsaacComputeRTXLidarFlatScan") or None


def build_stage_sensors(
    chassis_path: str, streams: dict[str, SensorStream]
) -> StageSensors:  # pragma: no cover - GPU path (Isaac authoring)
    """Author the camera / lidar prims under the chassis and bind the GT body.

    do-not-reinvent: the camera is ``isaacsim.sensors.camera.Camera`` (render
    product + rgb/depth annotators + intrinsics in one vendor object) and the
    lidar is ``isaacsim.sensors.rtx.LidarRtx`` with the stock
    ``IsaacComputeRTXLidarFlatScan`` annotator — we author no render graph.

    Both prims are CHILDREN of the chassis body, so the mounts are local offsets
    and the sensors follow the robot with no per-step transform writing at all.
    The camera's orientation is IDENTITY on purpose: ``Camera`` interprets its
    pose in ``camera_axes="world"`` (+X forward, +Z up), so identity already
    looks down the robot's nose — MEASURED, after passing the USD-frame
    quaternion produced a 90 deg-rotated image (probe A2 run 1).
    """
    from isaacsim.core.prims import SingleRigidPrim  # noqa: PLC0415
    from isaacsim.sensors.camera import Camera  # noqa: PLC0415
    from isaacsim.sensors.rtx import LidarRtx  # noqa: PLC0415

    sensors = StageSensors(body=SingleRigidPrim(chassis_path))
    camera_stream = (
        streams.get(STREAM_RGB) or streams.get(STREAM_DEPTH) or streams.get(STREAM_CAMERA_INFO)
    )
    if camera_stream is not None:
        sensors.camera = Camera(
            prim_path=f"{chassis_path}/{DEFAULT_CAMERA_FRAME}",
            name=DEFAULT_CAMERA_FRAME,
            resolution=CAMERA_RESOLUTION,
            translation=CAMERA_MOUNT_XYZ,
            orientation=(1.0, 0.0, 0.0, 0.0),
        )
    if STREAM_SCAN in streams:
        sensors.lidar = LidarRtx(
            prim_path=f"{chassis_path}/{DEFAULT_LIDAR_FRAME}",
            name=DEFAULT_LIDAR_FRAME,
            translation=LIDAR_MOUNT_XYZ,
            config_file_name=LIDAR_CONFIG,
        )
        sensors.lidar.attach_annotator("IsaacComputeRTXLidarFlatScan")
    return sensors


# --------------------------------------------------------------------------- #
# Vendor seam 2: the ROS message types (bundled internal Jazzy).
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class RosTypes:
    """The message classes + a QoS factory, injected so the suite stays pure."""

    Clock: type
    TFMessage: type
    TransformStamped: type
    Odometry: type
    Image: type
    CameraInfo: type
    LaserScan: type
    qos: Callable[..., object]


def import_ros_types() -> RosTypes:  # pragma: no cover - ROS path (bundled jazzy)
    """Import the bundled-Jazzy message types (deferred — R16, like the adapter)."""
    from geometry_msgs.msg import TransformStamped  # noqa: PLC0415
    from nav_msgs.msg import Odometry  # noqa: PLC0415
    from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy  # noqa: PLC0415
    from rosgraph_msgs.msg import Clock  # noqa: PLC0415
    from sensor_msgs.msg import CameraInfo, Image, LaserScan  # noqa: PLC0415
    from tf2_msgs.msg import TFMessage  # noqa: PLC0415

    def qos(depth: int = QOS_DEPTH, transient_local: bool = False) -> QoSProfile:
        return QoSProfile(
            depth=depth,
            reliability=ReliabilityPolicy.RELIABLE,
            durability=(
                DurabilityPolicy.TRANSIENT_LOCAL if transient_local else DurabilityPolicy.VOLATILE
            ),
        )

    return RosTypes(
        Clock=Clock,
        TFMessage=TFMessage,
        TransformStamped=TransformStamped,
        Odometry=Odometry,
        Image=Image,
        CameraInfo=CameraInfo,
        LaserScan=LaserScan,
        qos=qos,
    )


# --------------------------------------------------------------------------- #
# The suite.
# --------------------------------------------------------------------------- #
@dataclass
class _Publishers:
    """Created once in ``attach`` — one per topic this run actually publishes."""

    clock: object
    tf: object
    tf_static: object
    odom: list = field(default_factory=list)
    streams: dict = field(default_factory=dict)


class Go2SensorSuite:
    """Publishes the SUT-facing streams of a runner-composed world (D-2).

    Lifecycle, mirroring ``PhysicsTelemetrySampler``'s two phases because it has
    the same constraint (a tensor view created after ``world.reset()`` is already
    invalidated — p2c5 probe-03):

    * ``bind(world)`` — a ``SimRuntime.pre_reset`` hook: authors the camera/lidar
      prims and constructs the GT body view;
    * ``attach(node, on_step)`` — after ``adapter.wire()`` and **before the
      readiness barrier**: initializes the sensors, creates the publishers on the
      adapter's node, latches ``/tf_static`` and starts publishing. It has to be
      before the barrier because in this world WE are the ``/clock`` source, and
      the barrier waits for clock flow (G-19);
    * ``publish(sim_time)`` — the ``on_step`` callback.
    """

    def __init__(
        self,
        config: Ros2AdapterConfig,
        chassis_path: str,
        *,
        stage_factory: Callable[[str, dict], StageSensors] = build_stage_sensors,
        ros_types_factory: Callable[[], RosTypes] = import_ros_types,
    ) -> None:
        self.config = config
        self.chassis_path = chassis_path
        self.streams, self.unsupported = plan_sensor_streams(config.sensors)
        self._stage_factory = stage_factory
        self._ros_types_factory = ros_types_factory
        self._stage: StageSensors | None = None
        self._types: RosTypes | None = None
        self._pubs: _Publishers | None = None
        self._gates = {
            "odom": RateGate(ODOM_RATE_HZ),
            STREAM_RGB: RateGate(CAMERA_RATE_HZ),
            STREAM_DEPTH: RateGate(CAMERA_RATE_HZ),
            STREAM_CAMERA_INFO: RateGate(CAMERA_RATE_HZ),
            STREAM_SCAN: RateGate(SCAN_RATE_HZ),
        }
        self._data_gates = {
            STREAM_RGB: FirstDataGate(STREAM_RGB),
            STREAM_DEPTH: FirstDataGate(STREAM_DEPTH),
            STREAM_SCAN: FirstDataGate(STREAM_SCAN),
        }
        self._camera_info: CameraInfoFields | None = None
        self._world: object | None = None
        self.published: dict[str, int] = {}

    # --- lifecycle --------------------------------------------------------- #
    def bind(self, world: object) -> None:
        """Pre-reset hook: author the sensor prims (see ``build_stage_sensors``)."""
        if not self.chassis_path:
            raise RuntimeError(
                "go2 sensors need a chassis_path to mount on (no_collision criteria "
                "params / adapter_config) — scene-path hardcoding is forbidden (R7)"
            )
        self._world = world
        self._stage = self._stage_factory(self.chassis_path, self.streams)

    def attach(self, node: object, on_step: list | None = None) -> list[str]:
        """Create the publishers, latch ``/tf_static``, return the inventory lines."""
        if self._stage is None:
            raise RuntimeError("bind(world) must run as a pre-reset hook before attach()")
        self._types = self._ros_types_factory()
        self._stage.initialize()
        types = self._types
        self._pubs = _Publishers(
            clock=node.create_publisher(types.Clock, self.config.clock_topic, types.qos()),
            tf=node.create_publisher(types.TFMessage, TF_TOPIC, types.qos()),
            tf_static=node.create_publisher(
                types.TFMessage,
                TF_STATIC_TOPIC,
                types.qos(depth=TF_STATIC_QOS_DEPTH, transient_local=True),
            ),
            odom=[
                node.create_publisher(types.Odometry, topic, types.qos())
                for topic in self.config.odom_topics
            ],
            streams={
                key: node.create_publisher(self._message_type(key), stream.topic, types.qos())
                for key, stream in self.streams.items()
            },
        )
        if STREAM_CAMERA_INFO in self.streams:
            self._camera_info = camera_info_fields(*self._stage.calibration())
        self._publish_static_transforms()
        lines = inventory_lines(topic_inventory(self.config, self.streams))
        if not self.config.odom_topics:
            lines.append(
                "[cv-runner] WARNING: interface.adapter_config declares NO odom_topics — "
                "this world publishes no odometry at all, and a nav2 SUT cannot localise "
                "or drive without it"
            )
        if self.unsupported:
            lines.append(
                "[cv-runner] WARNING: declared sensor topic(s) this runner cannot "
                f"publish for a composed scene: {self.unsupported} (supported types: "
                f"{sorted(_SUPPORTED_TYPES)})"
            )
        if on_step is not None:
            on_step.append(self.publish_from_world)
        return lines

    def detach(self) -> list[str]:
        """Stop publishing; return the per-stream "did it ever carry data" lines."""
        self._pubs = None
        return [gate.summary() for key, gate in self._data_gates.items() if key in self.streams]

    # --- the step callback ------------------------------------------------- #
    def publish_from_world(self) -> None:
        """``SimRuntime.on_step`` listener: publish everything due at sim-time."""
        self.publish(float(self._world_time()))

    def publish(self, sim_time_s: float) -> None:
        """Publish ``/clock`` plus every stream due at ``sim_time_s`` (sim time)."""
        if self._pubs is None:
            return
        stamp = sim_time_stamp(sim_time_s)
        self._publish_clock(stamp)
        if self._gates["odom"].due(sim_time_s):
            self._publish_odometry(stamp)
        for key in (STREAM_RGB, STREAM_DEPTH, STREAM_CAMERA_INFO, STREAM_SCAN):
            if key in self.streams and self._gates[key].due(sim_time_s):
                self._publish_stream(key, stamp, sim_time_s)

    def _publish_stream(self, key: str, stamp: tuple[int, int], sim_time_s: float) -> None:
        if key == STREAM_CAMERA_INFO:
            self._publish_camera_info(stamp)
        elif key == STREAM_SCAN:
            self._publish_scan(stamp, sim_time_s)
        else:
            self._publish_image(key, stamp, sim_time_s)

    # --- message assembly -------------------------------------------------- #
    def _publish_clock(self, stamp: tuple[int, int]) -> None:
        msg = self._types.Clock()
        msg.clock.sec, msg.clock.nanosec = stamp
        self._emit("clock", self._pubs.clock, msg)

    def _publish_odometry(self, stamp: tuple[int, int]) -> None:
        position, quat = self._stage.pose()
        linear, angular = self._stage.velocity()
        fields = odom_fields(position, quat, linear, angular)
        frames = self.config.frames
        transform = self._transform_message(
            stamp,
            TransformFields(
                parent=frames.odom,
                child=frames.base_link,
                translation=fields.position,
                rotation_xyzw=fields.orientation_xyzw,
            ),
        )
        tf_msg = self._types.TFMessage()
        tf_msg.transforms = [transform]
        self._emit("tf", self._pubs.tf, tf_msg)
        for publisher in self._pubs.odom:
            msg = self._types.Odometry()
            self._stamp_header(msg, stamp, frames.odom)
            msg.child_frame_id = frames.base_link
            msg.pose.pose.position.x, msg.pose.pose.position.y, msg.pose.pose.position.z = (
                fields.position
            )
            (
                msg.pose.pose.orientation.x,
                msg.pose.pose.orientation.y,
                msg.pose.pose.orientation.z,
                msg.pose.pose.orientation.w,
            ) = fields.orientation_xyzw
            msg.twist.twist.linear.x, msg.twist.twist.linear.y, msg.twist.twist.linear.z = (
                fields.linear
            )
            msg.twist.twist.angular.x, msg.twist.twist.angular.y, msg.twist.twist.angular.z = (
                fields.angular
            )
            self._emit("odom", publisher, msg)

    def _publish_camera_info(self, stamp: tuple[int, int]) -> None:
        info = self._camera_info
        msg = self._types.CameraInfo()
        self._stamp_header(msg, stamp, self.streams[STREAM_CAMERA_INFO].frame)
        msg.height, msg.width = info.height, info.width
        msg.distortion_model = info.distortion_model
        msg.d = list(info.d)
        msg.k = list(info.k)
        msg.r = list(info.r)
        msg.p = list(info.p)
        self._emit(STREAM_CAMERA_INFO, self._pubs.streams[STREAM_CAMERA_INFO], msg)

    def _publish_image(self, key: str, stamp: tuple[int, int], sim_time_s: float) -> None:
        frame = self._stage.rgb() if key == STREAM_RGB else self._stage.depth()
        self._report(self._data_gates[key].observe(0 if frame is None else frame.size, sim_time_s))
        if frame is None:
            return
        height, width = int(frame.shape[0]), int(frame.shape[1])
        msg = self._types.Image()
        self._stamp_header(msg, stamp, self.streams[key].frame)
        msg.height, msg.width = height, width
        msg.encoding = RGB_ENCODING if key == STREAM_RGB else DEPTH_ENCODING
        msg.is_bigendian = 0
        msg.step = width * (3 if key == STREAM_RGB else 4)
        msg.data = frame.tobytes()
        self._emit(key, self._pubs.streams[key], msg)

    def _publish_scan(self, stamp: tuple[int, int], sim_time_s: float) -> None:
        frame = self._stage.scan()
        depths = [] if frame is None else list(frame.get("linearDepthData", []))
        self._report(self._data_gates[STREAM_SCAN].observe(len(depths), sim_time_s))
        if not depths:
            return
        fields = scan_fields(
            depths,
            frame["azimuthRange"],
            frame["horizontalResolution"],
            frame["depthRange"],
            1.0 / SCAN_RATE_HZ,
        )
        msg = self._types.LaserScan()
        self._stamp_header(msg, stamp, self.streams[STREAM_SCAN].frame)
        msg.angle_min = fields.angle_min
        msg.angle_max = fields.angle_max
        msg.angle_increment = fields.angle_increment
        msg.time_increment = fields.time_increment
        msg.scan_time = fields.scan_time
        msg.range_min = fields.range_min
        msg.range_max = fields.range_max
        msg.ranges = list(fields.ranges)
        self._emit(STREAM_SCAN, self._pubs.streams[STREAM_SCAN], msg)

    def _publish_static_transforms(self) -> None:
        transforms = static_transforms(self.config.frames.base_link, self.streams)
        if not transforms:
            return
        msg = self._types.TFMessage()
        # Sim time 0: /tf_static is latched (transient-local), so a late SUT node
        # gets it on subscription and tf2 treats a static transform as valid at
        # every time — the stamp is bookkeeping, not a lookup key.
        msg.transforms = [self._transform_message((0, 0), t) for t in transforms]
        self._emit("tf_static", self._pubs.tf_static, msg)

    # --- small shared helpers ---------------------------------------------- #
    def _transform_message(self, stamp: tuple[int, int], fields: TransformFields) -> object:
        msg = self._types.TransformStamped()
        self._stamp_header(msg, stamp, fields.parent)
        msg.child_frame_id = fields.child
        (
            msg.transform.translation.x,
            msg.transform.translation.y,
            msg.transform.translation.z,
        ) = fields.translation
        (
            msg.transform.rotation.x,
            msg.transform.rotation.y,
            msg.transform.rotation.z,
            msg.transform.rotation.w,
        ) = fields.rotation_xyzw
        return msg

    @staticmethod
    def _stamp_header(msg: object, stamp: tuple[int, int], frame_id: str) -> None:
        msg.header.stamp.sec, msg.header.stamp.nanosec = stamp
        msg.header.frame_id = frame_id

    def _message_type(self, key: str) -> type:
        types = self._types
        return {
            STREAM_RGB: types.Image,
            STREAM_DEPTH: types.Image,
            STREAM_CAMERA_INFO: types.CameraInfo,
            STREAM_SCAN: types.LaserScan,
        }[key]

    def _emit(self, key: str, publisher: object, msg: object) -> None:
        publisher.publish(msg)
        self.published[key] = self.published.get(key, 0) + 1

    @staticmethod
    def _report(line: str | None) -> None:
        if line is not None:
            print(line, flush=True)

    def _world_time(self) -> float:
        return self._world.current_time


def sensor_suite_for(
    scene_asset: object, config: Ros2AdapterConfig, chassis_path: str
) -> Go2SensorSuite | None:
    """The suite for a composed scene, or None for a pre-wired one (carter).

    ONE decision site for "does this run publish its own sensors", so the two
    entrypoints (a job and the dev world) cannot answer it differently.
    """
    if not scene_needs_runner_sensors(scene_asset):
        return None
    return Go2SensorSuite(config, chassis_path)
