"""Fresh-checkout setup and discovery checks, without Bluetooth or installation."""
import asyncio
import json
import os
from pathlib import Path
import plistlib
import subprocess
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from install import write_launchers
import install
from runtime_config import pinned_identifier, runtime_directory
import server

IDENTIFIER = "11111111-2222-3333-4444-555555555555"
DISCOVERY = [
    {"event": "connected", "id": IDENTIFIER},
    {"event": "service", "uuid": server.CONFIG["service_uuid"]},
]


class PortabilityTests(unittest.TestCase):
    def test_default_runtime_is_shared_user_state_not_checkout(self):
        with patch.dict(os.environ, {}, clear=True), patch("pathlib.Path.home", return_value=Path("/example/home")):
            self.assertEqual(runtime_directory(), Path("/example/home/Library/Application Support/BandBoxStudio/state"))

    def test_runtime_override_and_pinned_uuid(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
            "BANDBOX_RUNTIME_DIR": directory, "BANDBOX_DEVICE_UUID": IDENTIFIER,
        }, clear=True):
            self.assertEqual(runtime_directory(), Path(directory).resolve())
            self.assertEqual(pinned_identifier(), IDENTIFIER)
        with patch.dict(os.environ, {"BANDBOX_DEVICE_UUID": "invalid"}, clear=True):
            with self.assertRaisesRegex(ValueError, "CoreBluetooth UUID"):
                pinned_identifier()

    def test_launcher_and_client_example_use_this_checkout(self):
        with tempfile.TemporaryDirectory(prefix="bandbox setup ") as directory:
            root = Path(directory).resolve()
            app, config = write_launchers(root)
            data = json.loads(config.read_text())["mcpServers"]["jbl-bandbox"]
            self.assertEqual(data["command"], str(root / ".venv/bin/python"))
            self.assertEqual(data["args"], [str(root / "server.py")])
            script = app / "Contents/MacOS/launch"
            self.assertTrue(script.stat().st_mode & 0o100)
            self.assertNotIn(str(root), script.read_text())
            with (app / "Contents/Info.plist").open("rb") as source:
                self.assertEqual(plistlib.load(source)["CFBundleExecutable"], "launch")


class InstallerTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="bandbox-install-test-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        for target, value in (("ROOT", self.root),):
            patcher = patch.object(install, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        for target, kwargs in (
            ("install.sys.platform", {"new": "darwin"}),
            ("install.sys.version_info", {"new": (3, 11, 0)}),
            ("install.shutil.which", {"return_value": "/usr/bin/xcrun"}),
            ("install.subprocess.run", {}),
            ("install.venv.EnvBuilder", {}),
            ("install.write_launchers", {"return_value": ("app", "config")}),
            ("builtins.print", {}),
        ):
            patcher = patch(target, **kwargs)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_macos_environment_uses_symlinks(self):
        install.main()
        install.venv.EnvBuilder.assert_called_once_with(with_pip=True, symlinks=True)
        install.venv.EnvBuilder.return_value.create.assert_called_once_with(self.root / ".venv")

    def test_failed_environment_is_preserved_with_actionable_error(self):
        partial = self.root / ".venv" / "partial.txt"
        partial.parent.mkdir()
        partial.write_text("preserve me")
        install.venv.EnvBuilder.return_value.create.side_effect = subprocess.CalledProcessError(1, "ensurepip")
        with self.assertRaisesRegex(SystemExit, "venv/ensurepip step failed"):
            install.main()
        self.assertEqual(partial.read_text(), "preserve me")
        self.assertEqual(install.subprocess.run.call_count, 1)
        install.write_launchers.assert_not_called()

    def test_failed_pip_gives_repair_step_without_building(self):
        install.subprocess.run.side_effect = [None, subprocess.CalledProcessError(1, "pip")]
        with self.assertRaisesRegex(SystemExit, "pip --version works"):
            install.main()
        self.assertEqual(install.subprocess.run.call_count, 2)
        install.write_launchers.assert_not_called()


class DiscoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="bandbox-discovery-")
        self.addCleanup(self.directory.cleanup)
        for field, value in {
            "RUNTIME": Path(self.directory.name), "PINNED_ID": None,
            "runtime_id": None, "lock": asyncio.Lock(),
        }.items():
            patcher = patch.object(server, field, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    async def test_first_run_discovers_before_query_and_persists_verified_device(self):
        native = AsyncMock(side_effect=[(0, DISCOVERY, ""), (0, [{"event": "connected", "id": IDENTIFIER}], "")])
        with patch.object(server, "native", native):
            await server.run_probe("current")
        self.assertEqual(native.await_args_list[0].args, ("inspect", None, 30))
        self.assertEqual(native.await_args_list[1].args, ("current", IDENTIFIER, 12))
        self.assertEqual((server.RUNTIME / "device-id.txt").read_text().strip(), IDENTIFIER)

    async def test_wrong_service_does_not_persist_device_or_query(self):
        native = AsyncMock(return_value=(0, DISCOVERY[:1], ""))
        with patch.object(server, "native", native):
            result = await server.run_probe("current")
        self.assertEqual(native.await_count, 1)
        self.assertFalse((server.RUNTIME / "device-id.txt").exists())
        self.assertEqual(result["exit_code"], 1)

    async def test_explicit_device_pin_never_falls_back_to_another_amp(self):
        native = AsyncMock(return_value=(1, [], "offline"))
        with patch.object(server, "PINNED_ID", IDENTIFIER), patch.object(server, "native", native):
            result = await server.run_probe("inspect")
        native.assert_awaited_once_with("inspect", IDENTIFIER, 12)
        self.assertEqual(result["exit_code"], 1)

    async def test_cached_device_is_reused(self):
        (server.RUNTIME / "device-id.txt").write_text(IDENTIFIER)
        native = AsyncMock(return_value=(0, DISCOVERY, ""))
        with patch.object(server, "native", native):
            result = await server.run_probe("inspect")
        native.assert_awaited_once_with("inspect", IDENTIFIER, 12)
        self.assertEqual(result["active_corebluetooth_uuid"], IDENTIFIER)

    async def test_cached_device_is_ignored_when_channel_changed(self):
        # A device cached while BANDBOX_GUITAR_CHANNEL=3 (e.g. a reachable
        # Trio) must not be reused once the configured channel changes to 1;
        # otherwise channel-1 reads/writes would silently target that amp.
        (server.RUNTIME / "device-id.txt").write_text(IDENTIFIER)
        (server.RUNTIME / "device-channel.txt").write_text("3")
        other_id = "99999999-8888-7777-6666-555555555555"
        other_discovery = [
            {"event": "connected", "id": other_id},
            {"event": "service", "uuid": server.CONFIG["service_uuid"]},
        ]
        native = AsyncMock(return_value=(0, other_discovery, ""))
        with patch.object(server, "CHANNEL", 1), patch.object(server, "native", native):
            result = await server.run_probe("inspect")
        native.assert_awaited_once_with("inspect", None, 30)
        self.assertEqual(result["active_corebluetooth_uuid"], other_id)
        self.assertEqual((server.RUNTIME / "device-channel.txt").read_text().strip(), "1")


if __name__ == "__main__":
    unittest.main()
