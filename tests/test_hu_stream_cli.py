import base64
import contextlib
import hashlib
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import ANY, Mock, patch

import hu_stream


class StreamCliTests(unittest.TestCase):
    def test_unknown_host_key_can_be_added_to_known_hosts(self):
        class FakeParamiko:
            class MissingHostKeyPolicy:
                pass

            class SSHException(Exception):
                pass

        key = Mock()
        key.asbytes.return_value = b"host-public-key"
        key.get_name.return_value = "ssh-ed25519"
        client = Mock()
        messages = []
        with TemporaryDirectory() as directory:
            known_hosts = Path(directory) / ".ssh" / "known_hosts"
            policy = hu_stream._prompt_host_key_policy(
                FakeParamiko,
                known_hosts,
                input_fn=lambda prompt: "yes",
                print_fn=messages.append,
            )
            policy.missing_host_key(client, "192.0.2.10", key)

            fingerprint = base64.b64encode(
                hashlib.sha256(b"host-public-key").digest()
            ).decode("ascii").rstrip("=")
            client.get_host_keys().add.assert_called_once_with(
                "192.0.2.10", "ssh-ed25519", key,
            )
            client.save_host_keys.assert_called_once_with(str(known_hosts))
            self.assertTrue(known_hosts.parent.is_dir())
            self.assertIn(f"Fingerprint: SHA256:{fingerprint}", messages)
            self.assertIn("Added SSH host key", messages[-1])

    def test_declining_unknown_host_key_aborts(self):
        class FakeParamiko:
            class MissingHostKeyPolicy:
                pass

            class SSHException(Exception):
                pass

        client = Mock()
        key = Mock()
        key.asbytes.return_value = b"host-public-key"
        key.get_name.return_value = "ssh-ed25519"
        policy = hu_stream._prompt_host_key_policy(
            FakeParamiko,
            Path("known_hosts"),
            input_fn=lambda _prompt: "no",
            print_fn=lambda _message: None,
        )
        with self.assertRaisesRegex(FakeParamiko.SSHException, "connection aborted"):
            policy.missing_host_key(client, "192.0.2.10", key)
        client.get_host_keys().add.assert_not_called()
        client.save_host_keys.assert_not_called()

    def test_parse_resolution(self):
        self.assertEqual(hu_stream.parse_resolution("900x360"), (900, 360))
        self.assertEqual(hu_stream.parse_resolution("1280X720"), (1280, 720))

    def test_rejects_malformed_or_unsupported_resolution(self):
        for value in ("900", "0x360", "1281x360", "900x100"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                hu_stream.parse_resolution(value)

    def test_aborts_before_connecting_when_ssh_is_closed(self):
        progress = StringIO()
        with patch("hu_stream.check_ssh", side_effect=[False, False, False]) as check_ssh, \
                patch("hu_stream.time.sleep") as sleep, \
                patch("hu_stream.getpass") as password_prompt, \
                patch("hu_stream.HUScreenStream") as stream_class, \
                contextlib.redirect_stdout(progress):
            result = hu_stream.main(["--host", "192.0.2.10"])
        self.assertEqual(result, 1)
        self.assertEqual(check_ssh.call_count, 3)
        self.assertEqual(sleep.call_count, 2)
        self.assertEqual(progress.getvalue().splitlines(), [
            "Starting with checking if SSH is open.",
            "SSH is not open on 192.0.2.10:22; retrying",
            "retrying #2",
            "aborted",
        ])
        password_prompt.assert_not_called()
        stream_class.assert_not_called()

    def test_retries_ssh_check_until_head_unit_is_reachable(self):
        progress = StringIO()
        with patch("hu_stream.check_ssh", side_effect=[False, True]) as check_ssh, \
                patch("hu_stream.time.sleep") as sleep, \
                patch("hu_stream.getpass", return_value="secret"), \
                patch("hu_stream.HUScreenStream") as stream_class, \
                patch("hu_stream.display_stream"), \
                contextlib.redirect_stdout(progress):
            instance = stream_class.return_value
            instance.connect.return_value = (2, 2)
            instance.close.return_value = None
            result = hu_stream.main([])
        self.assertEqual(result, 0)
        self.assertEqual(check_ssh.call_count, 2)
        sleep.assert_called_once_with(hu_stream.SSH_RETRY_DELAY)
        self.assertIn(
            "SSH is not open on 169.254.199.119:22; retrying",
            progress.getvalue(),
        )
        self.assertIn("Port 22 reachable", progress.getvalue())

    def test_opens_stream_window_at_requested_resolution(self):
        progress = StringIO()
        with patch("hu_stream.check_ssh", return_value=True), \
                patch("hu_stream.getpass", return_value="secret") as password_prompt, \
                patch("hu_stream.HUScreenStream") as stream_class, \
                patch("hu_stream.display_stream") as display, \
                contextlib.redirect_stdout(progress):
            instance = stream_class.return_value
            instance.connect.return_value = (2, 2)
            instance.close.return_value = None
            result = hu_stream.main(["--resolution", "320x180"])
        self.assertEqual(result, 0)
        stream_class.assert_called_once_with(
            "169.254.199.119", "root", "secret", width=320, fps=30,
        )
        password_prompt.assert_called_once_with(
            "root@169.254.199.119 password: ",
        )
        display.assert_called_once_with(instance, 2, 2, 320, 180)
        instance.close.assert_called_once()
        self.assertIn("Port 22 reachable", progress.getvalue())
        self.assertIn("Listener running", progress.getvalue())

    def test_window_renders_frames_and_closes_on_escape(self):
        root = Mock()
        label = Mock()
        callbacks = []
        root.after.side_effect = lambda _delay, callback: callbacks.append(callback)
        root.mainloop.side_effect = lambda: callbacks.pop(0)()
        stream = Mock()
        stream.snapshot.return_value = (bytes((0, 0, 0) * 4), 28.5, 0, None)
        photo = object()
        with patch("tkinter.Tk", return_value=root), \
                patch("tkinter.Label", return_value=label) as label_class, \
                patch("PIL.ImageTk.PhotoImage", return_value=photo) as photo_image:
            hu_stream.display_stream(stream, 2, 2, 320, 180)
        root.geometry.assert_called_once_with("320x180")
        root.title.assert_any_call("HU Screen Stream - 28.5 FPS")
        root.iconphoto.assert_called_once_with(True, photo)
        photo_image.assert_any_call(file=str(hu_stream.ICON_PATH), master=root)
        root.bind.assert_called_once_with("<Escape>", ANY)
        label_class.assert_called_once_with(root, bd=0, highlightthickness=0)
        label.configure.assert_called_once_with(image=photo)
        self.assertIs(label.image, photo)
        root.bind.call_args.args[1](None)
        root.destroy.assert_called_once()


if __name__ == "__main__":
    unittest.main()
