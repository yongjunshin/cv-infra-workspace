"""SUT nav-state realign between batch samples (M2, p6 설계 정본 §0-9).

Between two samples of one request the SIM is put back to a declared start pose
(``SimRuntime.restage``) — but the SUT is a BLACKBOX that was never told, and it
is still holding the previous mission's beliefs: an AMCL particle cloud around
the old goal and two costmaps full of occupancy the robot has since teleported
away from. Driving sample i+1 on those beliefs measures the previous sample.

So this module re-seeds the SUT over its OWN published ROS interfaces — nothing
inside the SUT is modified, nothing is restarted (REQ-EXEC-005 blackbox stance):

(a) ``/initialpose`` — the exact topic RViz's "2D Pose Estimate" button
    publishes, with nav2's own default initial-pose covariance, and
(b) ``clear_entirely_{global,local}_costmap`` — nav2's own services.

Why it does NOT live in ``adapter/ros2.py``: this is nav2 KNOWLEDGE (topic,
service names, covariance layout), while the adapter is the interface-type seam
that adapter_config parameterizes. Keeping it separate is what lets a non-nav2
SUT ship its own realign without touching the adapter (NFR-EXEC-003).

The rclpy objects are created ONCE and OUTLIVE an iteration on purpose: a
publisher created and used in the same instant has not been DISCOVERED by the
subscriber yet, so the message goes nowhere and the realign reads as done while
doing nothing (G-26). ``realign`` therefore waits — BOUNDED — for a matched
subscription and REPORTS what it saw; the observation dict is the batch summary's
evidence that this step actually happened, so its keys are a contract.

The clock, by contrast, is read LIVE: every message of the burst is stamped with
the sim time it is published at, never with one time taken before the burst (see
``REALIGN_PUBLISH_COUNT`` for the measurement that says why). That is why the
sim-time source is an injected CALLABLE — a float parameter is exactly how the
stale-stamp bug was expressible.

WHAT pose the burst carries is decided by ``realign_seed`` (AR-19): the pose the
robot SETTLED at, not the coordinate the scenario declared.
"""

from __future__ import annotations

import time

from cv_infra.oracles.reached_goal import yaw_from_quat_wxyz
from cv_infra.runner.adapter.ros2 import quat_z_w_from_yaw

#: The AMCL pose-seed topic (nav2's own; RViz publishes the same one).
INITIALPOSE_TOPIC = "/initialpose"

#: nav2's own costmap-clearing services (blackbox-safe: published interfaces).
COSTMAP_CLEAR_SERVICES = (
    "/global_costmap/clear_entirely_global_costmap",
    "/local_costmap/clear_entirely_local_costmap",
)

#: ``/initialpose`` is a VOLATILE-durability topic: a late-joining or busy AMCL
#: can miss a single message and there is no re-delivery. Publishing a burst
#: (~0.5 sim-seconds at 60 Hz, one sim step between messages) is the cheap way to
#: make the seed land — but ONLY if every message carries its own stamp. nav2's
#: ``initialPoseReceived`` does not take the pose literally: it corrects it by the
#: odometry motion between ``header.stamp`` and now, so a burst sharing one stamp
#: taken right after a teleport folds that discontinuity into messages 2..N and
#: walks AMCL's belief back to where the robot used to be. Measured (p6c3 T3, the
#: same 12 samples, single-component control arm): one stamp -> belief within
#: 0.5 m at mission start **1/12**, verdict 3 pass / 9 fail, 31 planner failures
#: and 7 bt aborts in the SUT's own log; stamped per publish -> **12/12**,
#: 12 pass, 0 / 0.
REALIGN_PUBLISH_COUNT = 30

#: Wall bounds. Every wait here PUMPS the sim (the /clock source), so these are
#: caps on a productive loop, not sleeps — and every one of them is bounded
#: because a blackbox that never answers must degrade to a reported ``missing``
#: entry, never to a hung carrier (the M3 watchdog is the outer net, not this).
DISCOVERY_TIMEOUT_S = 10.0
SERVICE_READY_TIMEOUT_S = 5.0
SERVICE_CALL_TIMEOUT_S = 10.0

#: AMCL's default initial-pose covariance (nav2 ships x/y 0.25 m², yaw 0.0685
#: rad²) at their fixed indices in the row-major 6x6 covariance. Verbatim from
#: nav2's ``initial_pose`` defaults: a tighter guess would claim more confidence
#: than a teleport earns, a looser one would slow re-convergence.
INITIALPOSE_COVARIANCE = {
    0: 0.25,  # x
    7: 0.25,  # y
    35: 0.06853891945200942,  # yaw
}

#: The observation keys ``realign`` always returns. Fixed BOTH ways (a key that
#: appears only when it is interesting is a key nobody can count): the batch
#: summary carries this dict per sample, and NEG-6 counts the realign evidence
#: n/n from it, so a missing key would read as "n-1 samples realigned".
REALIGN_OBSERVATION_KEYS = (
    "initialpose_subscribers",
    "initialpose_published",
    "costmaps_cleared",
    "missing",
)


#: Where the ``/initialpose`` values came from — the tag the per-sample audit line
#: carries (AR-19). Three, because "we seeded the measured pose", "we fell back to
#: the declared one" and "we did not seed at all" are three different claims and a
#: reader of the runner log must be able to tell them apart without diffing numbers.
SEED_SOURCE_GT = "post-settle-GT"
SEED_SOURCE_DECLARED = "declared"
SEED_SOURCE_NONE = "not-declared"


def realign_seed(declared: dict | None, gt_sample: object | None) -> tuple[dict | None, str]:
    """The pose the ``/initialpose`` burst should carry, plus where it came from.

    AR-19. The declared coordinate is where the sim PUT the robot; it is not
    necessarily where the robot IS when the mission starts. Measured (U1 §6-1,
    §10-2): a go2 whose locomotion policy has just been handed the world lunges
    ~0.97 m in its first steps, and the lunge finishes BEFORE AMCL has seen enough
    odometry (``update_min_d`` 0.25) to correct itself — so every sample seeded
    with the declared pose began its mission believing it was ~1 m behind where
    it actually stood, which cost 6 recovery rounds and +75 s sim on the single-job
    reproduction. Seeding the post-settle GT pose is what an operator does with
    RViz's "2D Pose Estimate" button when they can see the robot: it tells the
    blackbox the truth, and it changes nothing INSIDE it (REQ-EXEC-005).

    The GT pose is the sim's own ``get_world_pose()`` sample (LOCKED §7) — never
    the SUT's ``/odom``, which is an INPUT to the very filter being seeded.

    Three arms, in the order they matter:

    * no declared pose -> ``(None, not-declared)``. The scenario asked for no
      initial pose, so ``restage`` restored the asset's own placement and
      ``SutRealigner.realign`` keeps its "not attempted" observation
      (``initialpose_subscribers = None``). Deliberately NOT "seed it from GT
      anyway": whether AMCL is re-seeded at all is the scenario's call, and a
      carrier that started seeding un-seeded scenarios would change the meaning
      of every batch that declares no pose.
    * declared, but nothing sampled yet -> the DECLARED pose, tagged as such. A
      sampler that produced no GT (telemetry never bound, a sim that never
      stepped) must not silently seed the origin.
    * declared + a GT sample -> the sampled x/y and its planar yaw.

    Yaw comes from the sample's quaternion through the oracle's own
    ``yaw_from_quat_wxyz`` (one spelling of "quaternion -> heading" in the
    codebase); the +Z-only convention on the way back out is
    ``initialpose_fields``'. Orientation is included because a legged robot's
    lunge turns it as well as moves it, and an AMCL seeded with the right
    position and the wrong heading re-converges no better than one seeded with
    neither.
    """
    if declared is None:
        return None, SEED_SOURCE_NONE
    if gt_sample is None:
        return dict(declared), SEED_SOURCE_DECLARED
    x, y, _z = gt_sample.position
    return (
        {"x": float(x), "y": float(y), "yaw": yaw_from_quat_wxyz(gt_sample.orientation_wxyz)},
        SEED_SOURCE_GT,
    )


def realign_seed_log(pose: dict | None, source: str) -> str:
    """The per-sample audit fragment: WHICH pose was seeded and where it came from.

    Appended to the carrier's ``sut realign:`` line so one grep answers "did this
    sample seed the measured pose?" — the observation dict counts messages, and a
    perfect count says nothing about the CONTENT (p6c3 T3 §9-2: counters 12/12
    while the belief was right 1/12). Kept as a pure function so the format is
    pinned by a CPU test instead of by an f-string nobody reads.
    """
    if pose is None:
        return f"realign_seed=none source={source}"
    return f"realign_seed=({pose['x']:.4f},{pose['y']:.4f},{pose['yaw']:.4f}) source={source}"


def initialpose_fields(pose: dict, sim_time_s: float) -> dict:
    """``{"x", "y", "yaw"}`` + sim-time -> the flat ``/initialpose`` field values.

    Pure (stdlib + the adapter's yaw->quaternion math), so the two things that
    are easy to get silently wrong are unit-tested on CPU:

    * the STAMP is sim-time split into (sec, nanosec) — D-F: the SUT runs on
      ``use_sim_time``, so a wall-clock stamp would be decades in its future and
      TF would drop the pose. The time is a PARAMETER (this function stays pure);
      the caller re-reads the clock for every message of the burst;
    * the ORIENTATION is planar (+Z only), the same convention
      ``initial_pose_world_transform`` writes into the sim, so the SUT is told
      exactly where the sim just put the robot.

    Returned as a flat dict rather than a message so the ROS type stays out of
    the CPU-testable surface (``apply_initialpose_fields`` writes it onto a real
    ``PoseWithCovarianceStamped``).
    """
    qz, qw = quat_z_w_from_yaw(float(pose["yaw"]))
    seconds = int(sim_time_s)
    return {
        "frame_id": "map",
        "stamp_sec": seconds,
        "stamp_nanosec": int((float(sim_time_s) - seconds) * 1e9),
        "position_x": float(pose["x"]),
        "position_y": float(pose["y"]),
        "orientation_z": qz,
        "orientation_w": qw,
        "covariance": dict(INITIALPOSE_COVARIANCE),
    }


def apply_initialpose_fields(msg: object, fields: dict) -> object:
    """Write ``initialpose_fields`` onto a ``PoseWithCovarianceStamped``-shaped msg.

    Attribute writes only — duck-typed on the message SHAPE, which is what makes
    the fill testable with a plain namespace on a host with no geometry_msgs.
    """
    msg.header.frame_id = fields["frame_id"]
    msg.header.stamp.sec = fields["stamp_sec"]
    msg.header.stamp.nanosec = fields["stamp_nanosec"]
    msg.pose.pose.position.x = fields["position_x"]
    msg.pose.pose.position.y = fields["position_y"]
    msg.pose.pose.orientation.z = fields["orientation_z"]
    msg.pose.pose.orientation.w = fields["orientation_w"]
    for index, value in fields["covariance"].items():
        msg.pose.covariance[index] = value
    return msg


class SutRealigner:
    """Re-seed the SUT's nav state between samples WITHOUT touching the SUT.

    Constructed ONCE per carrier and reused across iterations — see the module
    docstring on discovery. All three collaborators are injected rather than
    reached for: ``node`` is the adapter's public ``node`` property (one DDS
    participant per runner), ``step`` is its public ``step_and_spin`` (waiting
    without stepping would stall the /clock this very SUT is waiting on), and
    ``sim_time`` reads its public ``sim_time_s`` — a zero-arg callable, so the
    burst reads the clock as it advances instead of being handed a snapshot.

    The two ROS TYPES are injectable, defaulting to the bundled-Jazzy imports.
    That default IS the production path; the parameters exist so the observation
    logic — the part that decides "did this actually happen?" — is CPU-testable
    on a host with no ROS, with fakes standing in for the message and service.
    """

    def __init__(
        self,
        node: object,
        step,
        sim_time,
        *,
        pose_msg_type=None,
        clear_srv_type=None,
    ) -> None:
        self.node = node
        self.step = step
        self.sim_time = sim_time
        self._pose_msg_type = pose_msg_type
        self._clear_srv_type = clear_srv_type
        self._pub = None
        self._clients: dict = {}

    # -- lazily resolved ROS types (deferred: the bundled site is on sys.path
    # -- only after ros_bridge.bootstrap_bridge_env has run) ----------------- #
    def _pose_type(self):
        if self._pose_msg_type is None:  # pragma: no cover - ROS path
            from geometry_msgs.msg import PoseWithCovarianceStamped  # noqa: PLC0415

            self._pose_msg_type = PoseWithCovarianceStamped
        return self._pose_msg_type

    def _clear_type(self):
        if self._clear_srv_type is None:  # pragma: no cover - ROS path
            from nav2_msgs.srv import ClearEntireCostmap  # noqa: PLC0415

            self._clear_srv_type = ClearEntireCostmap
        return self._clear_srv_type

    # -- ROS collaborators, created once and kept (discovery — see module doc) #
    def _publisher(self):
        if self._pub is None:
            self._pub = self.node.create_publisher(self._pose_type(), INITIALPOSE_TOPIC, 10)
        return self._pub

    def _client(self, service: str):
        if service not in self._clients:
            self._clients[service] = self.node.create_client(self._clear_type(), service)
        return self._clients[service]

    # ----------------------------------------------------------------------- #
    def realign(self, pose: dict | None) -> dict:
        """Seed AMCL at ``pose`` and clear both costmaps; return what was OBSERVED.

        Never raises for a SUT that does not answer: a service that stays unready
        lands in ``missing`` and the sample runs anyway (its verdict then carries
        the consequence honestly). What WOULD be dishonest is claiming the realign
        happened, which is why every branch writes into the observation dict.

        ``pose=None`` (the scenario declares no initial pose, so the sim restored
        the asset's own placement): AMCL is not re-seeded — the robot is back
        where it started and re-seeding it with a pose we do not know would be an
        invention. The costmaps are still cleared: stale occupancy is stale either
        way. ``initialpose_subscribers`` stays None = "not attempted", which is
        NOT the same observation as 0 = "attempted, nobody was listening".
        """
        observed: dict = {
            "initialpose_subscribers": None,
            "initialpose_published": 0,
            "costmaps_cleared": [],
            "missing": [],
        }
        if self.node is None:
            observed["missing"].append("rclpy node (adapter not wired)")
            return observed

        if pose is not None:
            self._seed_initialpose(pose, observed)
        self._clear_costmaps(observed)
        return observed

    def _seed_initialpose(self, pose: dict, observed: dict) -> None:
        """Publish the ``/initialpose`` burst and record what the wire showed.

        Waits — BOUNDED, pumping the sim — for a matched subscription first: a
        publisher used in the instant it was created has not been DISCOVERED yet,
        so the seed goes nowhere while the counter says it was sent (G-26). The
        observed subscriber count is what tells those two apart afterwards.
        """
        pub = self._publisher()
        deadline = time.monotonic() + DISCOVERY_TIMEOUT_S
        while pub.get_subscription_count() == 0 and time.monotonic() < deadline:
            self.step()
        observed["initialpose_subscribers"] = pub.get_subscription_count()
        for _ in range(REALIGN_PUBLISH_COUNT):
            # Built fresh per message: the stamp must be the clock NOW, and a
            # burst is a burst of SEPARATE claims about the same pose, not one
            # message sent N times (see REALIGN_PUBLISH_COUNT).
            pub.publish(
                apply_initialpose_fields(
                    self._pose_type()(), initialpose_fields(pose, self.sim_time())
                )
            )
            self.step()
        observed["initialpose_published"] = REALIGN_PUBLISH_COUNT

    def _clear_costmaps(self, observed: dict) -> None:
        """Call nav2's own clear-entirely services; an unanswered one lands in ``missing``.

        Both waits (service readiness, then the call itself) are bounded and pump
        the sim: a blackbox that never answers must degrade to a reported entry,
        never to a hung carrier.
        """
        for service in COSTMAP_CLEAR_SERVICES:
            client = self._client(service)
            if not client.service_is_ready():
                ready_deadline = time.monotonic() + SERVICE_READY_TIMEOUT_S
                while not client.service_is_ready() and time.monotonic() < ready_deadline:
                    self.step()
            if not client.service_is_ready():
                observed["missing"].append(service)
                continue
            future = client.call_async(self._clear_type().Request())
            call_deadline = time.monotonic() + SERVICE_CALL_TIMEOUT_S
            while not future.done() and time.monotonic() < call_deadline:
                self.step()
            observed["costmaps_cleared" if future.done() else "missing"].append(service)
