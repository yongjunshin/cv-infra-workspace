"""Adapter contract schema (M1) — Phase 0 skeleton.

Versioned sub-schema for ``adapter_config`` when ``interface.type == "ros2"``
(REQ-EXEC-004/006). The recommended fields below are an information-item guide,
NOT a frozen wire schema (§1 deferral); the pydantic discriminated-union model
is finalized in Phase 3. Defaults track the LOCKED version pins (§7-1).

By design this schema has NO field that modifies the SUT container internals
(REQ-EXEC-005, blackbox SUT contract — the absence is itself the contract).
"""


class Ros2AdapterConfig:
    """``adapter_config`` sub-schema for interface.type=ros2 (skeleton).

    Phase-3 fields (placeholder, recommended):
      ros_distro     # "jazzy" (pinned)
      rmw            # "rmw_fastrtps_cpp" (FastDDS, pinned)
      use_sim_time   # true — SUT contract; verified at readiness, not forced
      urdf_ref       # optional; only if SUT omits robot_state_publisher
      topic_map      # sim<->SUT remap (no hardcoded topic names, R7)
      qos_overrides  # explicit QoS to avoid silent mismatch / single-link drop
      goal_interface # {kind: action|topic, name, type}
      readiness      # action server + /amcl_pose + lifecycle active + /clock flow
    """

    # Formalized as a pydantic v2 (discriminated-union) model in Phase 3.
    ...
