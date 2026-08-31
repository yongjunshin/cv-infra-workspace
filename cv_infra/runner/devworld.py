"""dev-world: the verification world, standing still, for a developer (M2, D-7).

``./python.sh -m cv_infra.runner.devworld <scenario.yaml>`` boots the SAME scene,
the SAME robot, the SAME locomotion policy and the SAME sensor publishers a
verification job would (``sim_runtime`` + ``go2_policy`` + ``go2_sensors``) — and
then just keeps stepping. No mission is driven, no oracle runs, nothing is
recorded and no result is written: the developer's own app is the thing under
test, and this is the world it talks to (plan §1-8: an app that only works
inside cv-infra is not the deliverable).

Reuse is the point, not a convenience: because the topic surface comes from the
same modules the job uses, "it worked in my dev world" and "it works in CI" mean
the same thing. The scenario file is admitted through the SAME 6-stage gate
(``contract.loader.load_request``), so a document that fails here fails there —
exit 2, before any GPU second is spent.

What it deliberately does NOT do: spawn the SUT (M3 owns containers, and here
the developer starts their own app themselves), evaluate, record, or write a
result.
"""

from __future__ import annotations

import signal
import sys
from dataclasses import dataclass

from cv_infra.contract.errors import ContractError
from cv_infra.contract.loader import AdmittedRequest, load_request
from cv_infra.runner.go2_policy import PolicyContractError
from cv_infra.runner.go2_wiring import PolicyPin, check_firmware_slot
from cv_infra.runner.main import (
    EXIT_PASS,
    EXIT_PLATFORM,
    EXIT_USAGE,
    criteria_view,
    hard_exit,
    obstacle_specs,
    plan_obstacle_pool,
    sim_config_for,
)
from cv_infra.runner.sim_runtime import EulaNotAcceptedError, resolve_scene

USAGE = "usage: ./python.sh -m cv_infra.runner.devworld <scenario.yaml> [--max-steps N]"


class DevWorldUsage(Exception):
    """Bad arguments or a rejected scenario — exit 2, the same as a job's."""


@dataclass(frozen=True)
class DevWorldArgs:
    """The parsed command line: one scenario, and an optional step bound."""

    scenario: str
    max_steps: int = 0  # 0 = run until Ctrl-C


def parse_args(argv: list[str]) -> DevWorldArgs:
    """``<scenario.yaml> [--max-steps N]`` -> args, or a friendly usage error.

    ``--max-steps`` exists so the dev world can be SMOKE-TESTED without a
    terminal (a bounded run that exits 0 on its own); the interactive default
    stays "until the developer stops it".
    """
    positional: list[str] = []
    max_steps = 0
    rest = list(argv)
    while rest:
        token = rest.pop(0)
        if token == "--max-steps":
            if not rest:
                raise DevWorldUsage(f"--max-steps needs a value. {USAGE}")
            value = rest.pop(0)
            try:
                max_steps = int(value)
            except ValueError as exc:
                raise DevWorldUsage(f"--max-steps must be an integer, got {value!r}") from exc
            if max_steps < 0:
                raise DevWorldUsage(f"--max-steps must be >= 0, got {max_steps}")
        elif token.startswith("-"):
            raise DevWorldUsage(f"unknown option {token!r}. {USAGE}")
        else:
            positional.append(token)
    if len(positional) != 1:
        raise DevWorldUsage(f"expected exactly one scenario file, got {positional}. {USAGE}")
    return DevWorldArgs(scenario=positional[0], max_steps=max_steps)


def admit(scenario_path: str) -> AdmittedRequest:
    """Run the scenario through the M1 admission gate (REQ-INTAKE-004/006/009).

    Same gate, same rejections, same exit code as a submitted job — including the
    ``sut.locomotion_policy`` digest check, which is what makes "the dev world
    ran the policy you are about to ship" a true statement rather than a hope.
    """
    try:
        return load_request(scenario_path)
    except ContractError as exc:
        raise DevWorldUsage(str(exc)) from exc


def policy_pin_for(admitted: AdmittedRequest) -> PolicyPin | None:
    """The admitted locomotion-policy pin, cross-checked against the scene's slot.

    The path is the LOADER's — resolved once, at the only place that knows the
    scenario's directory (blueprint §8) — so the dev world and a submitted job
    address the same file. The slot cross-check is C2b's
    (``go2_wiring.check_firmware_slot``): a go2 world with no policy stands up a
    robot with zero drive gains that simply lies down, and a policy declared for
    a robot that runs none is a request nobody can honour. Both are bad input
    here for the same reason they are in a job — exit 2, before the boot.
    """
    declared = admitted.request.sut.locomotion_policy
    pin = (
        None
        if declared is None or admitted.locomotion_policy_path is None
        else PolicyPin(admitted.locomotion_policy_path, declared.sha256)
    )
    try:
        check_firmware_slot(admitted.request, pin)
    except PolicyContractError as exc:
        raise DevWorldUsage(str(exc)) from exc
    return pin


def should_stop(steps: int, max_steps: int, stop_requested: bool) -> bool:
    """Loop predicate: Ctrl-C always wins; ``max_steps`` 0 means "no bound"."""
    return stop_requested or (max_steps > 0 and steps >= max_steps)


def banner(admitted: AdmittedRequest, policy: PolicyPin | None) -> list[str]:
    """What this world is, printed before the loop (there is no other UI)."""
    scenario = admitted.request.scenario
    return [
        "[cv-devworld] ready — the world is running; no mission, no oracle, no recording",
        f"[cv-devworld] scenario={admitted.source_path} scene={scenario.scene} "
        f"seed={scenario.seed}",
        f"[cv-devworld] locomotion_policy={'none' if policy is None else policy.path}",
        "[cv-devworld] drive it: ros2 topic pub -r 10 /cmd_vel geometry_msgs/msg/Twist "
        '"{linear: {x: 0.4}}"',
        "[cv-devworld] stop it: Ctrl-C",
    ]


def main(argv: list[str] | None = None) -> int:
    """Entry point: parse + admit on the CPU, then hand the GPU run its request.

    Only the two failures with a CONTRACT meaning are translated into an exit
    code (bad input = 2, missing EULA consent = 3, both the same as a job's).
    Anything else keeps its traceback: this is a developer's tool, and a stack
    trace is the most useful thing it can print when the world will not boot.
    """
    try:
        args = parse_args(sys.argv[1:] if argv is None else argv)
        admitted = admit(args.scenario)
        pin = policy_pin_for(admitted)
    except DevWorldUsage as exc:
        print(f"[cv-devworld] {exc}", file=sys.stderr, flush=True)
        return EXIT_USAGE
    try:
        return run(admitted, pin, args.max_steps)
    except EulaNotAcceptedError as exc:
        print(f"[cv-devworld] {exc}", file=sys.stderr, flush=True)
        return EXIT_PLATFORM


def run(
    admitted: AdmittedRequest, pin: PolicyPin | None = None, max_steps: int = 0
) -> int:  # pragma: no cover - GPU path
    """Boot the world and step it until Ctrl-C (or ``max_steps``).

    The order is ``main.run``'s, minus everything that belongs to judging a
    mission: bridge env -> SimulationApp -> bridge -> scene (+ obstacles +
    sensors as pre-reset hooks) -> wire -> sensors/policy attach -> loop. The
    readiness barrier is skipped on purpose — there may be no SUT at all yet, and
    the developer's app is expected to come and go while the world stays up.
    """
    from cv_infra.runner.adapter.ros2 import Ros2Adapter
    from cv_infra.runner.go2_sensors import sensor_suite_for
    from cv_infra.runner.go2_wiring import attach_policy_loop, load_policy, subscribe_cmd_vel
    from cv_infra.runner.ros_bridge import (
        bootstrap_bridge_env,
        enable_bridge,
        reexec_for_bridge_lib,
    )
    from cv_infra.runner.sim_runtime import SimRuntime

    request = admitted.request
    adapter_config = request.interface.adapter_config
    criteria = criteria_view(request)
    chassis_path = criteria.get("chassis_path", "")
    obstacles = obstacle_specs(request)
    pool = plan_obstacle_pool(obstacles)

    bootstrap = bootstrap_bridge_env(adapter_config.ros_distro, adapter_config.rmw)
    print(f"[cv-devworld] bridge bootstrap: {bootstrap}", flush=True)
    # The re-exec has to name THIS entry point and carry the same argv, or the
    # second process would boot a job runner with no JOB_SPEC (ros_bridge's
    # default argv is main's).
    reexec_for_bridge_lib(bootstrap, argv=[sys.executable, "-m", __spec__.name, *sys.argv[1:]])

    sim = SimRuntime(sim_config_for(request))
    adapter = Ros2Adapter(adapter_config, stepper=sim.step)
    sensors = sensor_suite_for(resolve_scene(request.scenario.scene), adapter_config, chassis_path)
    policy = load_policy(pin)  # digest re-verified before the GPU is paid
    stop = {"requested": False}

    def request_stop(_signum, _frame) -> None:
        stop["requested"] = True
        print("[cv-devworld] stop requested — leaving the world", flush=True)

    try:
        sim.boot()
        enable_bridge(sim.simulation_app)
        if pool:
            sim.pre_reset.append(lambda _world: sim.spawn_obstacle_pool(pool))
            sim.pre_reset.append(lambda _world: sim.apply_obstacle_set(obstacles))
        if sensors is not None:
            sim.pre_reset.append(sensors.bind)
        sim.load_scene()
        adapter.wire(sim.simulation_app, adapter_config)
        if sensors is not None:
            for line in sensors.attach(adapter.node, sim.on_step):
                print(line, flush=True)
        if policy is not None:
            # C2b's wiring, called not copied: same bind timing, same callback
            # name, same /cmd_vel semantics a judged job gets.
            attach_policy_loop(policy, sim)
            subscribe_cmd_vel(adapter.node, adapter_config.cmd_vel, policy.set_command)
        for line in banner(admitted, pin):
            print(line, flush=True)
        signal.signal(signal.SIGINT, request_stop)
        signal.signal(signal.SIGTERM, request_stop)
        steps = 0
        while not should_stop(steps, max_steps, stop["requested"]):
            adapter.step_and_spin()  # one sim step + a bounded rclpy drain
            steps += 1
        print(f"[cv-devworld] stepped {steps} time(s); shutting down", flush=True)
        if sensors is not None:
            for line in sensors.detach():
                print(line, flush=True)
        return EXIT_PASS
    finally:
        adapter.teardown()
        # Same G-62 deal as a job: the sim is NOT closed here (close() never
        # returns), so the exit code is delivered by process death in __main__.


if __name__ == "__main__":  # pragma: no cover
    hard_exit(main())  # NOT sys.exit: interpreter shutdown can still eat the code (G-62)
