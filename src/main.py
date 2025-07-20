import logging
import yaml
import asyncio
import tempfile
import os

from dglabv3 import dglabv3, Pulse, Channel
from osc import OSC
from dglab import Dglab_control


logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
log = logging.getLogger(__name__)
config = yaml.safe_load(open("config.yaml", encoding="utf-8"))


class DglabVrc:
    def __init__(self):
        self.osc = OSC()
        self.dglab = dglabv3()
        self.dglab_control = Dglab_control(self.dglab)
        self.main_loop = None

    async def run(self):
        log.info("Starting DglabVrc...")
        self.main_loop = asyncio.get_running_loop()

        await self.dglab.connect_and_wait()
        qr_bytes = self.dglab.generate_qrcode()

        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as temp_file:
            qr_bytes.seek(0)
            temp_file.write(qr_bytes.read())
            temp_file_path = temp_file.name

        os.startfile(temp_file_path)

        await self.dglab.wait_for_app_connect()
        log.info(config)
        self.register_handlers()
        asyncio.create_task(self.send_wave_task())
        self.dglab_control.start_reset_loop()
        while True:
            await asyncio.sleep(1)

    async def send_wave_task(self):
        while True:
            try:
                await self.dglab.send_wave_message(Pulse().breath, 10, Channel.BOTH)
            except Exception as e:
                log.error(f"Error sending wave message: {e}")
            await asyncio.sleep(15)

    def register_handlers(self):
        log.info(config.get("B", []))
        for path in config.get("A", []):
            log.info(f"Registering handler for path: {path}")
            self.osc.register_dispatcher(
                "/avatar/parameters/" + path, self.create_on_vrc_pb_handler("A")
            )
        for path in config.get("B", []):
            log.info(f"Registering handler for path: {path}")
            self.osc.register_dispatcher(
                "/avatar/parameters/" + path, self.create_on_vrc_pb_handler("B")
            )
        self.osc.register_dispatcher("/dglab/switch", self.on_vrc_switch_handler)

    def create_on_vrc_pb_handler(self, path):
        def handler(address, *args):
            log.info(f"Received OSC message for path: {path} with args: {args}")
            if self.main_loop and self.main_loop.is_running():
                asyncio.run_coroutine_threadsafe(
                    self.dglab_control.on_vrc_pb(path, *args),
                    self.main_loop,
                )
            else:
                log.error("Main event loop is not available")

        return handler

    def on_vrc_switch_handler(self, path, *args):
        self.dglab_control.enable = bool(args[0])
        log.info(f"Enabled Dglab control for path: {path}")


if __name__ == "__main__":
    dglab = DglabVrc()
    try:
        asyncio.run(dglab.run())
    except KeyboardInterrupt:
        log.info("Shutting down DglabVrc...")
        dglab.osc.close()
        log.info("DglabVrc has been shut down.")
        os._exit(0)
