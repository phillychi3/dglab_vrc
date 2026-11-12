import asyncio
import socket
import sys
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from dglabv3 import Channel, V4Protocol, dglabv3
from pythonosc.dispatcher import Dispatcher
from pythonosc.osc_message_builder import OscMessageBuilder
from pythonosc.udp_client import SimpleUDPClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from osc import OSC
from sps import SpsOvcControl, VRCFURY_ADDRESS
import main


class OscTransportTests(unittest.TestCase):
    def test_real_udp_receives_ogb_and_matches_handler(self):
        osc = OSC(listen_port=0)
        received = threading.Event()
        messages = []

        def handler(address, *args):
            messages.append((address, args))
            received.set()

        try:
            osc.register_dispatcher(VRCFURY_ADDRESS, handler)
            sender = SimpleUDPClient("127.0.0.1", osc.server.server_address[1])
            address = "/avatar/parameters/OGB/Orf/right_hand/PenOthersNewRoot"
            sender.send_message(address, 0.9)
            self.assertTrue(received.wait(2))
            self.assertEqual(messages[0][0], address)
            self.assertAlmostEqual(messages[0][1][0], 0.9)
            self.assertEqual(osc.received, 1)
            self.assertEqual(osc.ogb_received, 1)
        finally:
            sender._sock.close()
            osc.close()

    def test_occupied_port_raises_instead_of_silently_continuing(self):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as occupied:
            occupied.bind(("127.0.0.1", 0))
            with self.assertRaises(OSError):
                OSC(listen_port=occupied.getsockname()[1])


class SpsInputTests(unittest.IsolatedAsyncioTestCase):
    async def test_plug_log_values_drive_both_channels_without_touch(self):
        device = SimpleNamespace(slot_id="ovc", client_id="app", has_device=True,
                                 send_pulse=AsyncMock(), clear=AsyncMock())
        client = Mock()
        client.get_devices.return_value = [device]
        control = SpsOvcControl(client, {
            "enabled": True, "vrcfury_source": "plug",
        })
        prefix = "/avatar/parameters/OGB/Pen/Carrot/"
        control.update_depth(prefix + "PenOthers", 0.35966354608535767)
        control.update_depth(prefix + "PenSelf", 0.35966354608535767)
        control.update_depth(prefix + "TouchSelf", 0.7427122592926025)
        with self.assertLogs("sps", "DEBUG") as logs:
            await control.tick()
        self.assertIn("plug_inputs=1", " ".join(logs.output))
        self.assertIn("depth=0.360", " ".join(logs.output))
        self.assertEqual(len(control.plug_samples), 1)
        calls = device.send_pulse.await_args_list
        self.assertEqual([(call.args[0], call.args[1]) for call in calls], [
            (Channel.A, ["0A0A0A0A40404040"] * 3),
            (Channel.B, ["0A0A0A0A24242424"] * 3),
        ])

    async def test_plug_timeout_clears_both_outputs(self):
        device = SimpleNamespace(slot_id="ovc", client_id="app", has_device=True,
                                 send_pulse=AsyncMock(), clear=AsyncMock())
        client = Mock()
        client.get_devices.return_value = [device]
        control = SpsOvcControl(client, {
            "enabled": True,
        })
        with patch("sps.time.monotonic", return_value=10):
            control.update_depth("/avatar/parameters/OGB/Pen/Carrot/PenOthers", 0.5)
            await control.tick()
        with patch("sps.time.monotonic", return_value=12):
            await control.tick()
        self.assertFalse(control.plug_samples)
        stops = device.send_pulse.await_args_list[-2:]
        self.assertEqual({call.args[0] for call in stops}, {Channel.A, Channel.B})
        self.assertTrue(all(call.args[1] == ["0A0A0A0A00000000"] for call in stops))
        device.clear.assert_not_awaited()

    async def test_plug_self_filter_and_duplicate_values_use_maximum(self):
        for include_self, expected in ((False, 0.3), (True, 0.5)):
            control = SpsOvcControl(dglabv3(V4Protocol()), {
                "enabled": True, "include_self": include_self,
            })
            prefix = "/avatar/parameters/OGB/Pen/Carrot/"
            control.update_depth(prefix + "PenOthers", 0.3)
            control.update_depth(prefix + "PenSelf", 0.5)
            self.assertAlmostEqual(control._vrcfury_depth(time.monotonic()), expected)
            control.reset_inputs()
            self.assertFalse(control.plug_samples)

    async def test_vrcfury_source_selection(self):
        for source, expected_plugs, expected_sockets in (("auto", 1, 1), ("plug", 1, 0), ("socket", 0, 1)):
            control = SpsOvcControl(dglabv3(V4Protocol()), {
                "enabled": True, "vrcfury_source": source,
            })
            control.update_depth("/avatar/parameters/OGB/Pen/Carrot/PenOthers", 0.3)
            control.update_depth("/avatar/parameters/OGB/Orf/Down/PenOthersNewRoot", 0.9)
            self.assertEqual(len(control.plug_samples), expected_plugs)
            self.assertEqual(len(control.contacts), expected_sockets)
        with self.assertRaises(ValueError):
            SpsOvcControl(dglabv3(V4Protocol()), {"vrcfury_source": "other"})

    async def test_plug_osc_packet_routes_through_main(self):
        with patch.object(main, "OSC"), patch.object(main, "config", {
            "sps": {"enabled": True, "vrcfury_source": "plug"},
        }):
            app = main.DglabVrc()
            app.main_loop = asyncio.get_running_loop()
            dispatcher = Dispatcher()
            app.osc.register_dispatcher.side_effect = dispatcher.map
            app.register_handlers()
        packet = OscMessageBuilder(address="/avatar/parameters/OGB/Pen/Carrot/PenOthers")
        packet.add_arg(0.35966354608535767)
        dispatcher.call_handlers_for_packet(packet.build().dgram, ("127.0.0.1", 9001))
        await asyncio.sleep(0)
        await app.sps_control.tick()
        self.assertEqual(len(app.sps_control.plug_samples), 1)
        self.assertAlmostEqual(app.sps_control._vrcfury_depth(time.monotonic()), 0.35966354608535767)

    async def test_input_can_be_inspected_without_connected_ovc(self):
        client = dglabv3(V4Protocol())
        control = SpsOvcControl(client, {"enabled": True})
        for part, value in (("Root", 0.9), ("Tip", 1.0)):
            control.update_depth("/avatar/parameters/OGB/Orf/Down/PenOthersNew" + part, value)
        await control.tick()
        self.assertIn(("Down", "Others"), control.contacts)
        self.assertAlmostEqual(control._vrcfury_depth(time.monotonic()), 0.5)
        self.assertFalse(control.active)
        await control.tick(False)
        self.assertFalse(control.contacts)

    async def test_self_filter_is_reported(self):
        control = SpsOvcControl(dglabv3(V4Protocol()), {
            "enabled": True,
        })
        with self.assertLogs("sps", "DEBUG") as logs:
            control.update_depth("/avatar/parameters/OGB/Orf/Down/PenSelfNewRoot", 0.9)
        self.assertIn("include_self=false", " ".join(logs.output))
        self.assertFalse(control.contacts)

    async def test_ogb_enabled_and_handlers_start_before_pairing(self):
        entered_pairing = asyncio.Event()

        async def wait_for_pairing():
            entered_pairing.set()
            await asyncio.Event().wait()

        with patch.object(main, "OSC"), patch.object(main, "config", {
            "sps": {"enabled": True},
        }):
            app = main.DglabVrc()
        app.dglab.connect_and_wait = AsyncMock(side_effect=wait_for_pairing)
        task = asyncio.create_task(app.run())
        try:
            await entered_pairing.wait()
            await asyncio.sleep(0)
            app.osc.send_parameter.assert_any_call("OGB_ENABLED", True)
            self.assertTrue(app.osc.register_dispatcher.called)
        finally:
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

    async def test_osc_only_never_connects_to_dglab(self):
        with patch.object(main, "OSC"), patch.object(main, "config", {
            "sps": {"enabled": True},
        }):
            app = main.DglabVrc()
        app.dglab.connect_and_wait = AsyncMock()
        task = asyncio.create_task(app.run(osc_only=True))
        try:
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            app.dglab.connect_and_wait.assert_not_awaited()
            app.osc.send_parameter.assert_any_call("OGB_ENABLED", True)
            self.assertFalse(app.sps_control.active)
        finally:
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task


if __name__ == "__main__":
    unittest.main()
