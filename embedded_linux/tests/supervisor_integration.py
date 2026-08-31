#!/usr/bin/env python3
"""Host PTY integration test for motor-supervisor and motorctl."""

import errno
import importlib.util
import os
from pathlib import Path
import pty
import select
import shlex
import signal
import subprocess
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
SOURCES = ROOT / "meta-bldc/recipes-bldc/motor-control/files"
HARNESS = ROOT / "bldc_verification_harness/main.py"

spec = importlib.util.spec_from_file_location("motor_protocol", HARNESS)
protocol = importlib.util.module_from_spec(spec)
spec.loader.exec_module(protocol)


class Emulator(threading.Thread):
    def __init__(self, master_fd):
        super().__init__(daemon=True)
        self.master_fd = master_fd
        self.stop_event = threading.Event()
        self.respond_to_get_state = True
        self.buffer = bytearray()
        self.commands = []
        self.state = protocol.READY
        self.mode = protocol.LOCAL
        self.fault = protocol.NONE

    def stop(self):
        self.stop_event.set()
        self.join(timeout=1)

    def record(self, frame):
        command = frame[1]
        data = bytes(frame[2:6])
        self.commands.append((time.monotonic(), command, data))
        if command == protocol.GET_STATE and self.respond_to_get_state:
            response_data = bytes((self.state, self.mode, self.fault, 0))
            os.write(self.master_fd, protocol.build_frame(protocol.RSP_STATE, response_data))
        elif command == protocol.SET_MODE and self.state == protocol.READY:
            self.mode = data[0]
        elif command == protocol.ENABLE:
            if (
                self.state == protocol.READY
                and self.mode == protocol.REMOTE
                and self.fault == protocol.NONE
            ):
                self.state = protocol.RUN
        elif command == protocol.DISABLE and self.state == protocol.RUN:
            self.state = protocol.READY
        elif command == protocol.CLEAR_FAULT and self.state == protocol.FAULT:
            self.state = protocol.READY
            self.fault = protocol.NONE

    def run(self):
        os.set_blocking(self.master_fd, False)
        while not self.stop_event.is_set():
            readable, _, _ = select.select([self.master_fd], [], [], 0.02)
            if not readable:
                continue
            try:
                chunk = os.read(self.master_fd, 256)
            except OSError as error:
                if error.errno == errno.EIO:
                    time.sleep(0.01)
                    continue
                return
            if not chunk:
                continue
            self.buffer.extend(chunk)
            while len(self.buffer) >= protocol.FRAME_SIZE:
                try:
                    sof = self.buffer.index(protocol.SOF)
                except ValueError:
                    self.buffer.clear()
                    break
                if sof:
                    del self.buffer[:sof]
                if len(self.buffer) < protocol.FRAME_SIZE:
                    break
                frame = bytes(self.buffer[: protocol.FRAME_SIZE])
                if protocol.valid_frame(frame):
                    del self.buffer[: protocol.FRAME_SIZE]
                    self.record(frame)
                else:
                    del self.buffer[0]


def compile_programs(temp_dir, socket_path, lock_path):
    cc = shlex.split(os.environ.get("CC", "cc"))
    common = [
        "-D_POSIX_C_SOURCE=200809L",
        f'-DMOTOR_SOCKET_PATH="{socket_path}"',
        f'-DMOTOR_LOCK_PATH="{lock_path}"',
        "-O2",
        "-g",
        "-std=c11",
        "-Wall",
        "-Wextra",
        "-Wpedantic",
        "-Werror",
    ]
    supervisor = temp_dir / "motor-supervisor"
    motorctl = temp_dir / "motorctl"
    subprocess.run(
        cc
        + common
        + [
            str(SOURCES / "motor-supervisor.c"),
            str(SOURCES / "protocol.c"),
            "-o",
            str(supervisor),
        ],
        check=True,
    )
    subprocess.run(
        cc + common + [str(SOURCES / "motorctl.c"), "-o", str(motorctl)],
        check=True,
    )
    return supervisor, motorctl


def make_uart():
    master_fd, slave_fd = pty.openpty()
    slave_name = os.ttyname(slave_fd)
    os.close(slave_fd)
    emulator = Emulator(master_fd)
    emulator.start()
    return master_fd, slave_name, emulator


def wait_for_socket(process, socket_path):
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if process.poll() is not None:
            stderr = process.stderr.read()
            raise AssertionError(f"supervisor exited during startup:\n{stderr}")
        if socket_path.exists():
            return
        time.sleep(0.01)
    raise AssertionError("supervisor socket was not created")


def run_motorctl(motorctl, *arguments):
    result = subprocess.run(
        [str(motorctl), *arguments],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=2,
    )
    output = result.stdout if result.returncode == 0 else result.stderr
    if result.returncode != 0:
        raise AssertionError(
            f"motorctl {' '.join(arguments)} failed ({result.returncode}): {output}"
        )
    return output.strip()


def start_supervisor(supervisor, slave_name):
    return subprocess.Popen(
        [str(supervisor), "--device", slave_name],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )


def stop_process(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=1)


def start_uart_noise(master_fd, duration):
    def flood():
        deadline = time.monotonic() + duration
        noise = b"\x55" * 256

        while time.monotonic() < deadline:
            try:
                os.write(master_fd, noise)
            except BlockingIOError:
                time.sleep(0.001)

    thread = threading.Thread(target=flood, daemon=True)
    thread.start()
    return thread


def test_control_and_timing(supervisor, motorctl, socket_path):
    master_fd, slave_name, emulator = make_uart()
    process = start_supervisor(supervisor, slave_name)
    try:
        wait_for_socket(process, socket_path)
        time.sleep(0.15)
        startup_commands = [command for _, command, _ in emulator.commands]
        forbidden = {protocol.SET_MODE, protocol.ENABLE, protocol.CLEAR_FAULT}
        assert not forbidden.intersection(startup_commands), startup_commands

        assert "STATE=READY MODE=LOCAL FAULT=NONE" in run_motorctl(
            motorctl, "status"
        )
        assert "MODE=REMOTE" in run_motorctl(motorctl, "mode", "remote")
        assert "STATE=RUN" in run_motorctl(motorctl, "enable")
        assert "TARGET_SENT_NO_ACK" in run_motorctl(
            motorctl, "target", "30", "2000"
        )
        assert "STATE=READY" in run_motorctl(motorctl, "disable")

        timing_start = time.monotonic()
        noise_thread = start_uart_noise(master_fd, 0.6)
        time.sleep(1.7)
        noise_thread.join(timeout=1)
        timed = [item for item in emulator.commands if item[0] >= timing_start]
        heartbeats = [stamp for stamp, command, _ in timed if command == protocol.HEARTBEAT]
        queries = [item for item in timed if item[1] == protocol.GET_STATE]
        assert len(heartbeats) >= 12, len(heartbeats)
        assert len(queries) >= 3, len(queries)
        gaps = [right - left for left, right in zip(heartbeats, heartbeats[1:])]
        assert max(gaps) < 0.25, max(gaps)

        before_signal = len(emulator.commands)
        process.send_signal(signal.SIGTERM)
        assert process.wait(timeout=2) == 0
        shutdown_commands = [
            command for _, command, _ in emulator.commands[before_signal:]
        ]
        assert protocol.DISABLE in shutdown_commands, shutdown_commands
        stderr = process.stderr.read()
        assert "SIGINT/SIGTERM shutdown" in stderr, stderr
    finally:
        stop_process(process)
        emulator.stop()
        os.close(master_fd)


def test_three_query_timeouts(supervisor, socket_path):
    master_fd, slave_name, emulator = make_uart()
    process = start_supervisor(supervisor, slave_name)
    try:
        wait_for_socket(process, socket_path)
        deadline = time.monotonic() + 1
        while not any(command == protocol.GET_STATE for _, command, _ in emulator.commands):
            if time.monotonic() >= deadline:
                raise AssertionError("initial GET_STATE was not observed")
            time.sleep(0.01)
        time.sleep(0.05)
        emulator.respond_to_get_state = False
        return_code = process.wait(timeout=3)
        stderr = process.stderr.read()
        assert return_code != 0, return_code
        assert "GET_STATE timeout (3/3 consecutive)" in stderr, stderr
        assert "three consecutive GET_STATE timeouts" in stderr, stderr
    finally:
        stop_process(process)
        emulator.stop()
        os.close(master_fd)


def test_uart_disconnect(supervisor, socket_path):
    master_fd, slave_name, emulator = make_uart()
    process = start_supervisor(supervisor, slave_name)
    try:
        wait_for_socket(process, socket_path)
        deadline = time.monotonic() + 1
        while not any(command == protocol.GET_STATE for _, command, _ in emulator.commands):
            if time.monotonic() >= deadline:
                raise AssertionError("initial GET_STATE was not observed")
            time.sleep(0.01)
        emulator.stop()
        os.close(master_fd)
        master_fd = -1
        return_code = process.wait(timeout=2)
        stderr = process.stderr.read()
        assert return_code != 0, return_code
        assert "UART disconnected" in stderr, stderr
    finally:
        stop_process(process)
        emulator.stop()
        if master_fd >= 0:
            os.close(master_fd)


def main():
    # Some managed test sandboxes only permit Unix sockets below the workspace.
    with tempfile.TemporaryDirectory(prefix=".integration-", dir=ROOT) as name:
        temp_dir = Path(name)
        socket_path = temp_dir / "motor.sock"
        lock_path = temp_dir / "motor.lock"
        supervisor, motorctl = compile_programs(temp_dir, socket_path, lock_path)
        test_control_and_timing(supervisor, motorctl, socket_path)
        test_three_query_timeouts(supervisor, socket_path)
        test_uart_disconnect(supervisor, socket_path)
    print("supervisor PTY integration tests passed")


if __name__ == "__main__":
    main()
