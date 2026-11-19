"""Show the HU screen in a minimal live-stream window."""

import argparse
import base64
from getpass import getpass
import hashlib
from ipaddress import IPv4Address
from pathlib import Path
from PIL import Image
import socket
import struct
import sys
import threading
import time
from uuid import uuid4

DEFAULT_HU_HOST = "169.254.199.119"
DEFAULT_SSH_USER = "root"
SCREEN_PORT = 5555
DEFAULT_RESOLUTION = (900, 360)
DEFAULT_FPS = 30
SSH_CHECK_ATTEMPTS = 3
SSH_RETRY_DELAY = 1
SCREEN_FRAME_MAGIC = b"D8"
PREVIOUS_FRAME_MAGICS = (
    b"DF", b"D2", b"D3", b"D4", b"D5", b"D6", b"D7", b"D9", b"DB",
)
PF_ANCHOR = "filter_OBD"
PF_INTERFACE = "dwc0"
PF_FINAL_BLOCK = "block drop quick on dwc0 all"
HELPER_PATH = "/tmp/hu-screen-mirror"
HELPER_BINARY = Path(__file__).resolve().parent / "native" / "hu-screen-mirror"
ICON_PATH = Path(__file__).resolve().parent / "assets" / "hu-screen-streamer.png"


def rgb565_to_rgb888(data, width, height):
    if len(data) != width * height * 2:
        raise ValueError("Invalid RGB565 frame length")
    return Image.frombytes(
        "RGB", (width, height), data, "raw", "BGR;16"
    ).tobytes()


def build_pf_rules(rules_text, source, destination):
    """Insert one host-to-host allow rule before the existing final interface block."""
    source = str(IPv4Address(source))
    destination = str(IPv4Address(destination))
    rule = (
        f"pass in quick on {PF_INTERFACE} inet proto tcp from {source} "
        f"to {destination} port = {SCREEN_PORT} keep state"
    )
    lines = [line.rstrip() for line in rules_text.splitlines() if line.strip()]
    if rule in lines:
        return "\n".join(lines) + "\n", rule, False
    final_block = next(
        (index for index in range(len(lines) - 1, -1, -1)
         if lines[index].strip() == PF_FINAL_BLOCK),
        None,
    )
    if final_block is None:
        raise ValueError(
            f"Refusing to replace PF anchor {PF_ANCHOR}: "
            f"could not find its expected final rule: {PF_FINAL_BLOCK}"
        )
    lines.insert(final_block, rule)
    return "\n".join(lines) + "\n", rule, True


def remove_pf_rule(rules_text, rule):
    """Remove only this app's rule, preserving the rest of the active anchor."""
    lines = [line.rstrip() for line in rules_text.splitlines() if line.strip()]
    try:
        lines.remove(rule)
    except ValueError:
        return "\n".join(lines) + ("\n" if lines else ""), False
    if PF_FINAL_BLOCK not in lines:
        raise ValueError(
            f"Refusing to reload PF anchor {PF_ANCHOR}: "
            f"could not find its expected final rule: {PF_FINAL_BLOCK}"
        )
    return "\n".join(lines) + "\n", True


def _prompt_host_key_policy(paramiko, known_hosts, input_fn=None, print_fn=None):
    class PromptToTrustPolicy(paramiko.MissingHostKeyPolicy):
        def missing_host_key(self, client, hostname, key):
            fingerprint = base64.b64encode(
                hashlib.sha256(key.asbytes()).digest()
            ).decode("ascii").rstrip("=")
            show = print_fn or print
            prompt = input_fn or input
            show(f"SSH host key for {hostname} is not trusted.")
            show(f"Fingerprint: SHA256:{fingerprint}")
            answer = prompt(
                f"Add this key to {known_hosts}? [y/N]: "
            ).strip().casefold()
            if answer not in ("y", "yes"):
                raise paramiko.SSHException(
                    f"SSH host key for {hostname} was not added; connection aborted."
                )
            known_hosts.parent.mkdir(parents=True, exist_ok=True)
            client.get_host_keys().add(hostname, key.get_name(), key)
            client.save_host_keys(str(known_hosts))
            show(f"Added SSH host key to {known_hosts}")

    return PromptToTrustPolicy()


class HUScreenStream:
    """Own SSH, the temporary PF rule, and the HU's direct TCP screen stream."""

    def __init__(
        self, host, username, password=None, width=DEFAULT_RESOLUTION[0],
        fps=DEFAULT_FPS,
    ):
        self.host = str(IPv4Address(host))
        self.username = username
        self.password = password or None
        self.output_width = width
        self.width = width
        self.height = 0
        self.fps = fps
        self.frame_magic = SCREEN_FRAME_MAGIC
        self.ssh = None
        self.stdout = None
        self.stderr = None
        self.tcp = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._rgb565 = None
        self._dirty = False
        self._fps = 0.0
        self._mbps = 0.0
        self._error = None
        self._firewall_rule = None
        self._firewall_added = False
        self._receiver_thread = None
        self._pending_header = None
        self._closed = False

    def _remote_result(self, command, input_text=None, timeout=10):
        stdin, stdout, stderr = self.ssh.exec_command(command, timeout=timeout)
        if input_text is not None:
            stdin.write(input_text)
            stdin.flush()
        stdin.close()
        output = stdout.read()
        error = stderr.read()
        status = stdout.channel.recv_exit_status()
        return status, output.decode("utf-8", errors="replace"), error.decode(
            "utf-8", errors="replace"
        )

    def _read_pf_rules(self):
        status, output, error = self._remote_result(
            f"pfctl -a {PF_ANCHOR} -sr"
        )
        if status != 0:
            raise RuntimeError(
                f"Could not read the active PF anchor {PF_ANCHOR}: "
                f"{error.strip() or output.strip() or f'exit status {status}'}"
            )
        return output

    def _load_pf_rules(self, rules_text):
        temporary_path = f"/tmp/pf{uuid4().hex[:8]}"
        command = (
            f"umask 077; cat > {temporary_path} && "
            f"pfctl -a {PF_ANCHOR} -f {temporary_path}; "
            f"result=$?; rm -f {temporary_path}; exit $result"
        )
        status, output, error = self._remote_result(
            command, input_text=rules_text, timeout=15
        )
        if status != 0:
            raise RuntimeError(
                f"Could not load the temporary PF rule: "
                f"{error.strip() or output.strip() or f'exit status {status}'}"
            )

    def _install_firewall_rule(self, source):
        active_rules = self._read_pf_rules()
        updated_rules, rule, added = build_pf_rules(
            active_rules, source, self.host
        )
        self._firewall_rule = rule
        if not added:
            return
        self._load_pf_rules(updated_rules)
        self._firewall_added = True
        if rule not in self._read_pf_rules().splitlines():
            raise RuntimeError("PF accepted the reload but the screen rule is absent.")

    def _remove_firewall_rule(self):
        if not self._firewall_added or not self._firewall_rule:
            return
        active_rules = self._read_pf_rules()
        updated_rules, removed = remove_pf_rule(
            active_rules, self._firewall_rule
        )
        if removed:
            self._load_pf_rules(updated_rules)
        self._firewall_added = False

    def _upload_helper(self):
        if not HELPER_BINARY.is_file():
            raise FileNotFoundError(
                f"Native helper not found at {HELPER_BINARY}. "
                "Build it with native\\build.ps1 first."
            )
        command = f"umask 077; cat > {HELPER_PATH} && chmod 755 {HELPER_PATH}"
        stdin, stdout, stderr = self.ssh.exec_command(command, timeout=30)
        with HELPER_BINARY.open("rb") as helper:
            while True:
                chunk = helper.read(64 * 1024)
                if not chunk:
                    break
                stdin.write(chunk)
        stdin.flush()
        stdin.close()
        output = stdout.read()
        error = stderr.read()
        status = stdout.channel.recv_exit_status()
        if status != 0:
            raise RuntimeError(
                f"Could not upload the HU helper: "
                f"{error.decode('utf-8', errors='replace').strip() or output.decode('utf-8', errors='replace').strip() or f'exit status {status}'}"
            )

    def _attach_to_running_helper(self, upload_error):
        try:
            self.tcp = socket.create_connection(
                (self.host, SCREEN_PORT), timeout=5
            )
            self.tcp.settimeout(5)
            header = self._recv_exact(8)
            magic, width, height, changed = struct.unpack("<2sHHH", header)
            known_magics = PREVIOUS_FRAME_MAGICS + (SCREEN_FRAME_MAGIC,)
            if magic in known_magics and (
                magic != self.frame_magic
                or width != self.output_width
            ):
                self.tcp.close()
                self.tcp = None
                return False
            if (
                magic != self.frame_magic
                or width != self.output_width
                or not 1 <= height <= 2160
                or changed > height
            ):
                raise ValueError("Existing HU helper sent an invalid frame header")
            self.width, self.height = width, height
            self._pending_header = header
            self.tcp.settimeout(None)
            return True
        except Exception as exc:
            if self.tcp is not None:
                self.tcp.close()
                self.tcp = None
            raise RuntimeError(
                f"{upload_error}; could not reuse the existing HU helper: {exc}"
            ) from exc

    def _upload_helper_after_stale_process(self):
        for attempt in range(10):
            try:
                self._upload_helper()
                return
            except RuntimeError as exc:
                if "resource busy" not in str(exc).casefold():
                    raise
                if attempt == 9:
                    raise RuntimeError(
                        f"{exc}; the outdated HU helper did not release its executable"
                    ) from exc
                time.sleep(0.2)

    def connect(self):
        import paramiko

        try:
            client = paramiko.SSHClient()
            self.ssh = client
            known_hosts = Path.home() / ".ssh" / "known_hosts"
            if known_hosts.is_file():
                client.load_host_keys(str(known_hosts))
            client.load_system_host_keys()
            client.set_missing_host_key_policy(
                _prompt_host_key_policy(paramiko, known_hosts)
            )
            client.connect(
                self.host,
                username=self.username,
                password=self.password,
                allow_agent=False,
                look_for_keys=False,
                timeout=10,
                banner_timeout=10,
                auth_timeout=10,
            )
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as route:
                route.connect((self.host, 22))
                local_address = route.getsockname()[0]
            print("Opening the 5555 port on the firewall on the HU", flush=True)
            self._install_firewall_rule(local_address)
            start_helper = True
            try:
                print("Uploading QNX Video helper to RAM", flush=True)
                self._upload_helper()
            except RuntimeError as exc:
                if "resource busy" not in str(exc).casefold():
                    raise
                start_helper = not self._attach_to_running_helper(exc)
                if start_helper:
                    self._upload_helper_after_stale_process()
            if start_helper:
                command = (
                    f"exec {HELPER_PATH} --tcp {SCREEN_PORT} {self.fps} "
                    f"{self.output_width} {self.host} --rgb565"
                )
                _, self.stdout, self.stderr = self.ssh.exec_command(
                    command, timeout=15
                )
                self.stdout.channel.settimeout(15)
                ready = self.stdout.readline().strip()
                parts = ready.split()
                if len(parts) != 4 or parts[0] != "READY":
                    details = self.stderr.read().decode(
                        "utf-8", errors="replace"
                    ).strip()
                    raise RuntimeError(
                        f"Unexpected HU helper startup output: {ready!r}"
                        + (f" ({details})" if details else "")
                    )
                self.width, self.height, port = map(int, parts[1:])
                if self.width != self.output_width or not 1 <= self.height <= 2160 or port != SCREEN_PORT:
                    raise ValueError(
                        f"HU helper returned unexpected stream parameters: "
                        f"{self.width}x{self.height} on port {port}"
                    )
                self.tcp = socket.create_connection(
                    (self.host, port), timeout=5
                )
            self.tcp.settimeout(None)
            self.tcp.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self._rgb565 = bytearray(self.width * self.height * 2)
            self._dirty = True
            self._receiver_thread = threading.Thread(
                target=self._receive, name="hu-screen-receiver", daemon=True
            )
            self._receiver_thread.start()
            return self.width, self.height
        except Exception as exc:
            cleanup_error = self.close()
            if cleanup_error:
                raise RuntimeError(f"{exc}; cleanup failed: {cleanup_error}") from exc
            raise

    def _receive(self):
        frames = wire_bytes = 0
        reported = time.monotonic()
        try:
            while not self._stop.is_set():
                header = self._pending_header
                self._pending_header = None
                if header is None:
                    header = self._recv_exact(8)
                magic, width, height, changed = struct.unpack("<2sHHH", header)
                if magic != self.frame_magic or width != self.width or height != self.height:
                    raise ValueError("Corrupt HU screen frame header")
                if changed > self.height:
                    raise ValueError("HU screen frame has too many changed rows")
                wire_row_bytes = self.width * 2
                wire_bytes += len(header)
                if changed:
                    payload = self._recv_exact(changed * (2 + wire_row_bytes))
                    wire_bytes += len(payload)
                    offset = 0
                    with self._lock:
                        for _ in range(changed):
                            row, = struct.unpack_from("<H", payload, offset)
                            if row >= self.height:
                                raise ValueError(f"HU frame row out of bounds: {row}")
                            start = row * wire_row_bytes
                            self._rgb565[start:start + wire_row_bytes] = payload[
                                offset + 2:offset + 2 + wire_row_bytes
                            ]
                            offset += 2 + wire_row_bytes
                        self._dirty = True
                frames += 1
                now = time.monotonic()
                if now - reported >= 1:
                    with self._lock:
                        self._fps = frames / (now - reported)
                        self._mbps = wire_bytes * 8 / (
                            (now - reported) * 1_000_000
                        )
                    frames = wire_bytes = 0
                    reported = now
        except (OSError, EOFError, ValueError, struct.error) as exc:
            if not self._stop.is_set():
                with self._lock:
                    self._error = str(exc)

    def _recv_exact(self, size):
        data = bytearray(size)
        view = memoryview(data)
        received = 0
        while received < size:
            if self._stop.is_set():
                raise EOFError("HU stream stopped")
            count = self.tcp.recv_into(view[received:])
            if count == 0:
                raise EOFError("HU closed the TCP stream")
            received += count
        return data

    def snapshot(self):
        with self._lock:
            rgb565 = (
                bytes(self._rgb565)
                if self._dirty and self._rgb565 is not None
                else None
            )
            self._dirty = False
            fps, mbps, error = self._fps, self._mbps, self._error
        frame = (
            rgb565_to_rgb888(rgb565, self.width, self.height)
            if rgb565 is not None else None
        )
        return frame, fps, mbps, error

    def close(self):
        if self._closed:
            return None
        self._closed = True
        self._stop.set()
        if self.tcp is not None:
            self.tcp.close()
            self.tcp = None
        if self.stdout is not None:
            self.stdout.channel.close()
            self.stdout = None
        self.stderr = None
        if self._receiver_thread is not None:
            self._receiver_thread.join(timeout=2)
        cleanup_error = None
        if self.ssh is not None:
            try:
                self._remove_firewall_rule()
            except Exception as exc:
                cleanup_error = str(exc)
            finally:
                self.ssh.close()
                self.ssh = None
        return cleanup_error


def parse_resolution(value):
    try:
        width_text, height_text = value.lower().split("x", 1)
        width, height = int(width_text), int(height_text)
    except (AttributeError, ValueError):
        raise ValueError("Resolution must use WIDTHxHEIGHT, for example 900x360") from None
    if not 320 <= width <= 1280 or not 180 <= height <= 2160:
        raise ValueError("Resolution width must be 320-1280 and height 180-2160")
    return width, height


def check_ssh(host, port=22, timeout=3):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def display_stream(stream, source_width, source_height, width, height):
    import tkinter as tk
    from PIL import ImageTk

    root = tk.Tk()
    root.title("HU Screen Stream")
    root.geometry(f"{width}x{height}")
    root.resizable(False, False)
    icon = ImageTk.PhotoImage(file=str(ICON_PATH), master=root)
    root.iconphoto(True, icon)
    root._stream_icon = icon
    image_label = tk.Label(root, bd=0, highlightthickness=0)
    image_label.pack(fill=tk.BOTH, expand=True)
    state = {"error": None}

    def render_frame():
        try:
            frame, fps, _, error = stream.snapshot()
            if error:
                state["error"] = error
                root.destroy()
                return
            title = f"HU Screen Stream - {fps:.1f} FPS"
            if title != state.get("title"):
                root.title(title)
                state["title"] = title
            if frame is not None:
                image = Image.frombytes("RGB", (source_width, source_height), frame)
                if image.size != (width, height):
                    image = image.resize((width, height), Image.Resampling.BILINEAR)
                photo = ImageTk.PhotoImage(image, master=root)
                image_label.configure(image=photo)
                image_label.image = photo
            root.after(15, render_frame)
        except Exception as exc:
            state["error"] = str(exc)
            root.destroy()

    root.protocol("WM_DELETE_WINDOW", root.destroy)
    root.bind("<Escape>", lambda _event: root.destroy())
    root.after(0, render_frame)
    root.mainloop()
    if state["error"]:
        raise RuntimeError(state["error"])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=DEFAULT_HU_HOST, help="HU IPv4 address (default: %(default)s)")
    parser.add_argument("--username", default=DEFAULT_SSH_USER, help="SSH username (default: %(default)s)")
    parser.add_argument("--resolution", type=parse_resolution, default=DEFAULT_RESOLUTION,
                        metavar="WIDTHxHEIGHT", help="stream window size (default: 900x360)")
    parser.add_argument("--fps", type=int, default=DEFAULT_FPS, help="capture frame rate, 1-60 (default: %(default)s)")
    args = parser.parse_args(argv)
    try:
        host = str(IPv4Address(args.host))
    except ValueError as exc:
        parser.error(str(exc))
    if not 1 <= args.fps <= 60:
        parser.error("--fps must be 1-60")
    print("Starting with checking if SSH is open.", flush=True)
    for attempt in range(SSH_CHECK_ATTEMPTS):
        if check_ssh(host):
            break
        if attempt == SSH_CHECK_ATTEMPTS - 1:
            print("aborted", flush=True)
            return 1
        if attempt == 0:
            print(f"SSH is not open on {host}:22; retrying", flush=True)
        else:
            print(f"retrying #{attempt + 1}", flush=True)
        time.sleep(SSH_RETRY_DELAY)

    print("Port 22 reachable", flush=True)
    password = getpass(f"{args.username}@{host} password: ")
    width, height = args.resolution
    stream = HUScreenStream(host, args.username, password, width=width, fps=args.fps)
    try:
        source_width, source_height = stream.connect()
        print("Listener running", flush=True)
        display_stream(stream, source_width, source_height, width, height)
    except KeyboardInterrupt:
        print("\nStream stopped.")
    except Exception as exc:
        print(f"Stream aborted: {exc}", file=sys.stderr)
        return 1
    finally:
        cleanup_error = stream.close()
        if cleanup_error:
            print(f"Warning: could not remove temporary HU firewall rule: {cleanup_error}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
