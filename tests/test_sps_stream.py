import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from dglabv3 import Channel, V4Protocol, dglabv3

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from sps import SpsOvcControl


class StreamTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.protocol = V4Protocol()
        self.client = dglabv3(self.protocol)
        app = self.protocol._add_app("app")
        app.replace_devices([{
            "slotId": "ovc", "type": "OVC_1", "slotState": {"hasDevice": True},
        }])
        self.now = 0.0
        self.frames = []
        self.ends = {}
        self.gaps = []

        async def send(frame):
            request = frame["data"]
            self.frames.append(request)
            if request["m"] == "device.op":
                op = request["data"]
                channel = op["c"]
                # A minimal App playback model: frame count and duration both
                # bound playback; immediate requests replace the current task.
                if channel in self.ends and self.now > self.ends[channel] + 1e-9:
                    self.gaps.append(self.now - self.ends[channel])
                self.ends[channel] = self.now + min(len(op["v"]) * 100, op["d"]) / 1000
            self.protocol._resolve_response("app", {"reqId": request["reqId"], "result": {}})

        self.protocol._send_frame = send
        self.control = SpsOvcControl(self.client, {
            "enabled": True, "vrcfury_source": "plug",
        })

    async def asyncTearDown(self):
        await self.client.close()

    def update(self, depth):
        self.control.update_depth("/avatar/parameters/OGB/Pen/Test/PenOthers", depth)

    async def test_buffer_covers_jitter_and_always_replaces_old_depth(self):
        # These arrivals leave gaps with one 100 ms frame per request.
        for arrival, depth in ((0, .25), (.14, .5), (.27, .75), (.46, .6)):
            self.now = arrival
            self.update(depth)
            await self.control.tick()
        self.assertEqual(self.gaps, [])
        self.assertEqual(len(self.frames), 8)
        for frame in self.frames:
            op = frame["data"]
            self.assertEqual(op["d"], 300)
            self.assertEqual(len(op["v"]), 3)
            self.assertTrue(op["im"])
        self.assertEqual(self.frames[-1]["data"]["v"], ["0A0A0A0A3C3C3C3C"] * 3)

    async def test_zeroing_one_channel_does_not_wait_for_clear_ack(self):
        device = self.control._device()
        device.clear = AsyncMock(side_effect=AssertionError("must not wait on clear"))
        self.update(.5)
        await self.control.tick()
        self.frames.clear()
        self.update(1)
        await asyncio.wait_for(self.control.tick(), .2)
        ops = [frame["data"] for frame in self.frames]
        self.assertEqual(ops[0]["c"], 0)
        self.assertEqual(ops[0]["v"], ["0A0A0A0A00000000"])
        self.assertEqual(ops[0]["d"], 100)
        self.assertEqual(ops[1]["c"], 1)
        self.assertEqual(ops[1]["v"], ["0A0A0A0A64646464"] * 3)
        device.clear.assert_not_awaited()

    async def test_disable_replaces_both_channels_with_zero(self):
        self.update(.5)
        await self.control.tick()
        self.frames.clear()
        await self.control.tick(False)
        self.assertEqual({frame["data"]["c"] for frame in self.frames}, {0, 1})
        self.assertTrue(all(frame["data"]["v"] == ["0A0A0A0A00000000"] for frame in self.frames))
        self.assertFalse(self.control.active)

    async def test_fixed_buffer_and_user_amplitude_limits(self):
        self.control = SpsOvcControl(self.client, {
            "enabled": True, "max_amplitude": 80,
        })
        self.update(.5)
        await self.control.tick()
        self.assertEqual(self.frames[-1]["data"]["d"], 300)
        self.assertEqual(len(self.frames[-1]["data"]["v"]), 3)
        self.assertEqual(self.frames[-1]["data"]["v"][0], "0A0A0A0A28282828")
        for value in (-1, 101):
            with self.assertRaises(ValueError):
                SpsOvcControl(self.client, {"max_amplitude": value})

    async def test_schedule_accounts_for_send_time_and_skips_missed_slots(self):
        real_sleep = asyncio.sleep
        clock = [0.0]
        waits = []
        done = asyncio.Event()
        count = [0]

        async def tick(enabled=True):
            if not enabled:
                return
            count[0] += 1
            clock[0] += .03 if count[0] == 1 else .24

        async def sleep(delay):
            waits.append(delay)
            clock[0] += delay
            if len(waits) >= 2:
                done.set()
                await asyncio.Event().wait()
            await real_sleep(0)

        loop = asyncio.get_running_loop()
        self.control.tick = tick
        # Override only the clock used by the controller, not asyncio's clock.
        fake_loop = type("Clock", (), {"time": lambda _: clock[0]})()
        with patch("sps.asyncio.get_running_loop", return_value=fake_loop), \
                patch("sps.asyncio.sleep", side_effect=sleep):
            task = loop.create_task(self.control.run(lambda: True))
            try:
                await done.wait()
                self.assertAlmostEqual(waits[0], .07)
                self.assertAlmostEqual(waits[1], .06)
                self.assertEqual(count[0], 2)
            finally:
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task


if __name__ == "__main__":
    unittest.main()
