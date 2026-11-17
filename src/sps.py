import asyncio
import logging
import math
import re
import time

from dglabv3 import Channel, Ovc

log = logging.getLogger(__name__)
FRAME_SECONDS = 0.1
WAVE_BUFFER_MS = 300
INPUT_TIMEOUT_SECONDS = 1.0
ACTIVATION_DEPTH = 0.1
FALLBACK_PLUG_LENGTH_METERS = 0.2
VRCFURY_ADDRESS = "/avatar/parameters/OGB/Orf/*/Pen*New*"
VRCFURY_PLUG_ADDRESS = "/avatar/parameters/OGB/Pen/*/Pen*"
VRCFURY_ADDRESSES = (VRCFURY_ADDRESS, VRCFURY_PLUG_ADDRESS)
VRCFURY_CONTACT = re.compile(
    r"/avatar/parameters/OGB/Orf/([^/]+)/Pen(Self|Others)New(Root|Tip)"
)
VRCFURY_PLUG = re.compile(r"/avatar/parameters/OGB/Pen/([^/]+)/Pen(Self|Others)")


def depth_frame(depth, max_amplitude=100):
    """One 100 ms frame: four unused frequency bytes, four 25 ms amplitudes."""
    amplitude = round(max(0.0, min(1.0, depth)) * max_amplitude)
    return (bytes([10] * 4) + bytes([amplitude] * 4)).hex().upper()


def depth_position(depth, shallow_channel=Channel.A, activation_depth=0.1):
    """Crossfade shallow/deep outputs, with a short ramp from no contact."""
    depth = max(0.0, min(1.0, depth))
    level = min(1.0, depth / activation_depth)
    deep_channel = Channel.B if shallow_channel == Channel.A else Channel.A
    return {
        shallow_channel: level * (1.0 - depth),
        deep_channel: level * depth,
    }


class SpsOvcControl:
    def __init__(self, client, settings):
        self.client = client
        self.enabled = settings.get("enabled", False)
        self._last_report = -float("inf")
        self._self_reported = False
        self.shallow_channel = Channel[settings.get("shallow_channel", "A")]
        if self.shallow_channel not in (Channel.A, Channel.B):
            raise ValueError("sps.shallow_channel must be A or B")
        self.vrcfury_source = settings.get("vrcfury_source", "auto")
        if self.vrcfury_source not in ("auto", "plug", "socket"):
            raise ValueError("sps.vrcfury_source must be auto, plug or socket")
        self.include_self = settings.get("include_self", False)
        self.max_amplitude = int(settings.get("max_amplitude", 100))
        if not 0 <= self.max_amplitude <= 100:
            raise ValueError("sps.max_amplitude must be between 0 and 100")
        self.plug_samples = {}
        self.contacts = {}
        self.lengths = {}
        self.active = {}
        # Latest computed values, read by the GUI.
        self.last_depth = 0.0
        self.last_levels = {Channel.A: 0.0, Channel.B: 0.0}

    def update_depth(self, address, *args):
        """Called on the main asyncio loop; coalesce OSC bursts into one sample."""
        if not self.enabled or len(args) != 1:
            return
        value = args[0]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return
        if not math.isfinite(value):
            return
        sample = (max(0.0, min(1.0, value)), time.monotonic())
        match = (
            VRCFURY_CONTACT.fullmatch(address)
            if self.vrcfury_source != "plug"
            else None
        )
        plug_match = (
            VRCFURY_PLUG.fullmatch(address) if self.vrcfury_source != "socket" else None
        )
        if match or plug_match:
            party = (match or plug_match).group(2)
            if party == "Self" and not self.include_self:
                if log.isEnabledFor(logging.DEBUG) and not self._self_reported:
                    log.debug("SPS Self input ignored: include_self=false")
                    self._self_reported = True
                return
            if match:
                socket, party, part = match.groups()
                self.contacts.setdefault((socket, party), {})[part] = sample
            else:
                # Legacy Plug receiver is centered at the base with radius
                # equal to plug length: proximity approximates insertion 0..1.
                self.plug_samples[address] = sample

    def reset_inputs(self):
        self.plug_samples.clear()
        self.contacts.clear()
        self.lengths.clear()

    def _vrcfury_depth(self, now):
        depth = 0.0
        for address, (value, received) in list(self.plug_samples.items()):
            if now - received > INPUT_TIMEOUT_SECONDS:
                del self.plug_samples[address]
            else:
                # Self and Others may both report world contacts. Use max,
                # never sum them, so duplicate reports do not double output.
                depth = max(depth, value)
        for key, contacts in list(self.contacts.items()):
            # OSC can report only changed values: a saturated Tip may stay at 1
            # while Root continues moving. Expire the pair on its latest update.
            if (
                now - max(received for _, received in contacts.values())
                > INPUT_TIMEOUT_SECONDS
            ):
                del self.contacts[key]
                self.lengths.pop(key, None)
                continue
            if "Root" not in contacts or "Tip" not in contacts:
                continue
            root, _ = contacts["Root"]
            tip, _ = contacts["Tip"]
            # VRCFury uses 1 m proximity receivers: distance = 1 - contact.
            # Before insertion, the difference measures the plug's length.
            if 0 < root < tip < 0.999:
                self.lengths[key] = tip - root
            if tip < 0.999 or root <= 0:
                continue
            length = self.lengths.get(key, FALLBACK_PLUG_LENGTH_METERS)
            depth = max(depth, max(0.0, min(1.0, 1 - (1 - root) / length)))
        return depth

    def _device(self):
        return next(
            (device for device in self.client.get_devices(Ovc) if device.has_device),
            None,
        )

    async def tick(self, output_enabled=True):
        device = self._device() if self.enabled and output_enabled else None
        if not self.enabled or not output_enabled:
            self.reset_inputs()
        now = time.monotonic()
        vrcfury_depth = self._vrcfury_depth(now)
        depths = depth_position(vrcfury_depth, self.shallow_channel, ACTIVATION_DEPTH)
        self.last_depth = vrcfury_depth
        self.last_levels = depths

        if log.isEnabledFor(logging.DEBUG) and now - self._last_report >= 5:
            log.debug(
                "SPS status: contact_pairs=%s plug_inputs=%s depth=%.3f A=%.2f B=%.2f Ovc=%s enabled=%s",
                len(self.contacts),
                len(self.plug_samples),
                vrcfury_depth,
                depths[Channel.A],
                depths[Channel.B],
                None if device is None else device.slot_id,
                output_enabled,
            )
            self._last_report = now

        # Release channels on the previous device before selecting a new one.
        for channel, previous in list(self.active.items()):
            if (
                previous is not device
                or depths[channel] == 0
                or self.max_amplitude == 0
            ):
                try:
                    if previous.has_device and self.client.is_linked_to_app():
                        # Acknowledged clears can stall the other channel.
                        # Replace only the wave task, without waiting for a reply.
                        await previous.send_pulse(
                            channel,
                            [depth_frame(0)],
                            duration=100,
                            immediate=True,
                            wait=False,
                        )
                except Exception:
                    log.exception("Could not clear SPS Ovc channel %s", channel.name)
                    continue
                del self.active[channel]

        if device is None:
            return
        for channel, depth in depths.items():
            if depth == 0 or self.max_amplitude == 0:
                continue
            if channel in self.active and self.active[channel] is not device:
                continue
            try:
                # Cover network jitter until the next 100 ms refresh arrives.
                # Replacement prevents buffering old depths behind the new one.
                await device.send_pulse(
                    channel,
                    [depth_frame(depth, self.max_amplitude)] * (WAVE_BUFFER_MS // 100),
                    duration=WAVE_BUFFER_MS,
                    immediate=True,
                    wait=False,
                )
                self.active[channel] = device
            except Exception:
                log.exception("Could not send SPS wave to Ovc channel %s", channel.name)

    async def run(self, output_enabled):
        loop = asyncio.get_running_loop()
        deadline = loop.time()
        try:
            while True:
                await self.tick(output_enabled())
                deadline += FRAME_SECONDS
                now = loop.time()
                if deadline < now:
                    # Skip missed refreshes, avoiding drift and catch-up bursts.
                    deadline += (
                        math.floor((now - deadline) / FRAME_SECONDS) + 1
                    ) * FRAME_SECONDS
                await asyncio.sleep(max(0.0, deadline - loop.time()))
        finally:
            await self.tick(False)
