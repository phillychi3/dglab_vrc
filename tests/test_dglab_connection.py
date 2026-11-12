import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import main


class DglabConnectionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        with patch.object(main, "OSC"), patch.object(main, "config", {}):
            self.app = main.DglabVrc()
        self.client = self.app.dglab
        self.protocol = self.client.protocol
        self.protocol.target_id = "test-controller"
        await self.protocol._handle_frame({"type": "client_attached", "clientId": "test-app"})
        await self.protocol._on_app_message("test-app", {
            "t": "ev", "ev": "devices.snapshot", "devices": [],
        })

    async def asyncTearDown(self):
        await self.client.close()

    async def test_empty_device_list_does_not_block_normal_mode(self):
        self.client.connect_and_wait = AsyncMock()
        wait_for_app = AsyncMock(wraps=self.client.wait_for_app_connect)
        self.client.wait_for_app_connect = wait_for_app
        self.protocol.request_devices = AsyncMock(return_value=[])
        output_started = asyncio.Event()

        async def output_loop(*args):
            output_started.set()
            await asyncio.Event().wait()

        self.app.sps_control.run = output_loop
        self.app.send_wave_task = AsyncMock(side_effect=output_loop)
        self.app.dglab_control.start_reset_loop = Mock()
        # Avoid opening a QR image or writing a file during the regression test.
        with patch.object(main.os, "startfile", create=True), \
                patch.object(main.tempfile, "NamedTemporaryFile"):
            task = asyncio.create_task(self.app.run())
            try:
                await asyncio.wait_for(output_started.wait(), 1)
                wait_for_app.assert_awaited_once_with(timeout=120, wait_device=False)
                self.assertTrue(self.client.is_linked_to_app())
                self.assertEqual(self.client.devices, [])
                self.assertFalse(task.done())
            finally:
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task

    async def test_device_discovery_timeout_keeps_app_connected(self):
        self.protocol.request_devices = AsyncMock(side_effect=TimeoutError("no device reply"))
        with self.assertLogs(main.log, "WARNING") as logs:
            task = asyncio.create_task(self.app.monitor_devices())
            try:
                await asyncio.sleep(0)
                self.protocol.request_devices.assert_awaited_once()
                self.assertTrue(self.client.is_linked_to_app())
                self.assertFalse(task.done())
            finally:
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
        self.assertIn("will retry", " ".join(logs.output))

    async def test_explicit_discovery_accepts_late_ovc_snapshot(self):
        async def reply(app):
            await self.protocol._on_app_message(app.client_id, {
                "t": "ev", "ev": "devices.snapshot", "devices": [{
                    "slotId": "ovc-slot", "type": "OVC_1",
                    "slotState": {"hasDevice": True},
                }],
            })
            return self.client.devices

        self.protocol.request_devices = AsyncMock(side_effect=reply)
        task = asyncio.create_task(self.app.monitor_devices())
        try:
            await asyncio.sleep(0)
            self.protocol.request_devices.assert_awaited_once()
            self.assertTrue(self.client.is_linked_to_app())
            self.assertEqual(self.app.sps_control._device().slot_id, "ovc-slot")
        finally:
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task


if __name__ == "__main__":
    unittest.main()
