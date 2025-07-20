from dglabv3 import dglabv3, Channel
import logging
import time
import asyncio

log = logging.getLogger(__name__)

MAX_STRENGTH = 30


class Dglab_control:
    def __init__(self, dgclass: dglabv3):
        self.enable = True
        self.last_send_time = {Channel.A: 0, Channel.B: 0}
        self.send_delay = 0.5
        self.dgclass = dgclass
        self.reset_loop_enabled = False
        self.reset_interval = 5.0
        self.reset_task = None

    async def reset_to_zero(self):
        if not self.dgclass.is_linked_to_app():
            return
        if self.last_send_time[Channel.A] - time.time() > self.send_delay:
            await self.dgclass.set_strength_value(channel=Channel.A, strength=0)
        if self.last_send_time[Channel.B] - time.time() > self.send_delay:
            await self.dgclass.set_strength_value(channel=Channel.B, strength=0)

    async def continuous_reset_loop(self):
        while self.reset_loop_enabled:
            await self.reset_to_zero()
            await asyncio.sleep(self.reset_interval)

    def start_reset_loop(self):
        if not self.reset_loop_enabled:
            self.reset_loop_enabled = True
            self.reset_task = asyncio.create_task(self.continuous_reset_loop())

    def stop_reset_loop(self):
        if self.reset_loop_enabled:
            self.reset_loop_enabled = False
            if self.reset_task and not self.reset_task.done():
                self.reset_task.cancel()

    async def on_vrc_pb(
        self,
        path: str,
        value,
    ):
        if not self.enable:
            return

        log.info(f"Sending command for path: {path} with value: {value}")

        if path == "A":
            channel = Channel.A
        elif path == "B":
            channel = Channel.B
        else:
            return

        current_time = time.time()
        if current_time - self.last_send_time[channel] < self.send_delay:
            log.debug(f"Skipping command for channel {channel.name} due to rate limit")
            return

        self.last_send_time[channel] = current_time

        target_strength = int(MAX_STRENGTH * float(value))
        log.info(f"Setting strength for channel {channel.name} to {target_strength}")

        try:
            log.info(
                f"About to call set_strength_value with channel={channel.name}, value={target_strength}"
            )
            await self.dgclass.set_strength_value(
                channel=channel, strength=target_strength
            )
            log.info(
                f"Successfully called set_strength_value for channel {channel.name}"
            )
        except Exception as e:
            log.error(f"Error calling set_strength_value: {e}")
            import traceback

            log.error(f"Full traceback: {traceback.format_exc()}")
