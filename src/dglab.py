from dglabv3 import dglabv3, Channel
import logging
import time

log = logging.getLogger(__name__)

MAX_STRENGTH = 10


class Dglab_control:
    def __init__(self):
        self.enable = True
        self.last_send_time = {Channel.A: 0, Channel.B: 0}
        self.send_delay = 0.5

    async def on_vrc_pb(
        self,
        path: str,
        dgclass: dglabv3,
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
            await dgclass.set_strength_value(channel=channel, strength=target_strength)
            log.info(
                f"Successfully called set_strength_value for channel {channel.name}"
            )
        except Exception as e:
            log.error(f"Error calling set_strength_value: {e}")
            import traceback

            log.error(f"Full traceback: {traceback.format_exc()}")
