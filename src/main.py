import logging
import yaml
import asyncio
import tempfile
import os
import argparse

from dglabv3 import dglabv3, Pulse, Channel, Coyote, V4Protocol
from osc import OSC
from dglab import Dglab_control
from sps import SpsOvcControl, VRCFURY_ADDRESSES


logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
log = logging.getLogger(__name__)


def configure_logging(debug=False):
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
                        force=True)
    for name in (__name__, "osc", "sps", "dglab", "dglabv3"):
        logging.getLogger(name).setLevel(logging.DEBUG if debug else logging.INFO)


with open("config.yaml", encoding="utf-8") as config_file:
    config = yaml.safe_load(config_file)


class DglabVrc:
    def __init__(self):
        self.osc = OSC()
        self.dglab = dglabv3(V4Protocol())
        self.dglab_control = Dglab_control(self.dglab)
        self.sps_control = SpsOvcControl(self.dglab, config.get("sps", {}))
        self.main_loop = None

    async def run(self, osc_only=False):
        log.info("Starting DglabVrc...")
        self.main_loop = asyncio.get_running_loop()
        self.register_handlers()
        # Enable VRChat contacts while waiting for DG-LAB pairing, too.
        tasks = [asyncio.create_task(self.maintain_osc())]
        try:
            if osc_only:
                log.info("OSC-only diagnostic mode; no DG-LAB connection or device output")
            else:
                await self.dglab.connect_and_wait()
                log.info("DG-LAB relay connected; scan the QR code using DG-LAB 4 App")
                qr_bytes = self.dglab.generate_qrcode()
                with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as temp_file:
                    qr_bytes.seek(0)
                    temp_file.write(qr_bytes.read())
                    temp_file_path = temp_file.name
                os.startfile(temp_file_path)
                tasks.append(asyncio.create_task(self.monitor_devices()))
                # 0.3.0 otherwise waits for a non-empty device list too, then
                # closes a healthy App connection if the list stays empty.
                app = await self.dglab.wait_for_app_connect(timeout=120, wait_device=False)
                log.info("DG-LAB App connected: %s; waiting for available devices", app.client_id)
                tasks.append(asyncio.create_task(self.send_wave_task()))
                tasks.append(asyncio.create_task(
                    self.sps_control.run(lambda: self.dglab_control.enable)
                ))
                self.dglab_control.start_reset_loop()
            while True:
                if osc_only:
                    # Compute diagnostics without selecting any output device.
                    await self.sps_control.tick(self.dglab_control.enable)
                await asyncio.sleep(1)
        finally:
            self.dglab_control.stop_reset_loop()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if self.sps_control.enabled:
                self.osc.send_parameter("OGB_ENABLED", False)
            await self.dglab.close()

    async def monitor_devices(self):
        """Discover devices without making App pairing depend on device readiness."""
        previous = None
        next_refresh = 0.0
        loop = asyncio.get_running_loop()
        while True:
            apps = self.dglab.apps
            devices = self.dglab.devices
            state = (
                tuple(app.client_id for app in apps),
                tuple((device.client_id, device.slot_id, device.type, device.has_device)
                      for device in devices),
            )
            if state != previous:
                log.info("DG-LAB connection: apps=%s devices=%s", len(apps), len(devices))
                for device in devices:
                    log.info("DG-LAB device: type=%s slot=%s connected=%s",
                             device.type, device.slot_id, device.has_device)
                if apps and not devices:
                    log.warning("DG-LAB App is connected but its device list is empty; keeping connection open")
                previous = state

            # Some Apps do not push an initial snapshot. Ask explicitly, and
            # retry if none was supplied; a discovery timeout is not a pairing failure.
            if apps and not devices and loop.time() >= next_refresh:
                for app in apps:
                    try:
                        await self.dglab.protocol.request_devices(app)
                    except Exception as exc:
                        log.warning("DG-LAB device discovery failed: %s: %s; will retry",
                                    type(exc).__name__, exc)
                next_refresh = loop.time() + 10
            await asyncio.sleep(1)

    async def maintain_osc(self):
        counter = 0
        while True:
            if self.sps_control.enabled:
                self.osc.send_parameter("OGB_ENABLED", self.dglab_control.enable)
                if counter % 5 == 0:
                    log.debug("OSC OGB_ENABLED sent: %s", self.dglab_control.enable)
            if counter % 5 == 0:
                self.osc.report_status()
            counter += 1
            await asyncio.sleep(1)

    async def send_wave_task(self):
        while True:
            try:
                device = self.dglab.get_device(Coyote)
                if device is not None and device.has_device:
                    await device.send_wave(Pulse().breath, 10, Channel.BOTH)
            except Exception as e:
                log.error(f"Error sending wave message: {e}")
            await asyncio.sleep(15)

    def register_handlers(self):
        for path in config.get("A", []):
            log.debug("Registering handler for path: %s", path)
            self.osc.register_dispatcher(
                "/avatar/parameters/" + path, self.create_on_vrc_pb_handler("A")
            )
        for path in config.get("B", []):
            log.debug("Registering handler for path: %s", path)
            self.osc.register_dispatcher(
                "/avatar/parameters/" + path, self.create_on_vrc_pb_handler("B")
            )
        self.osc.register_dispatcher("/dglab/switch", self.on_vrc_switch_handler)
        self.osc.register_dispatcher("/avatar/change", self.on_avatar_change_handler)
        if self.sps_control.enabled:
            for address in VRCFURY_ADDRESSES:
                self.osc.register_dispatcher(address, self.on_sps_depth_handler)

    def on_sps_depth_handler(self, address, *args):
        if self.main_loop and self.main_loop.is_running():
            self.main_loop.call_soon_threadsafe(
                self.sps_control.update_depth, address, *args
            )

    def on_avatar_change_handler(self, address, *args):
        if self.main_loop and self.main_loop.is_running():
            self.main_loop.call_soon_threadsafe(
                self.set_enabled, self.dglab_control.enable
            )

    def create_on_vrc_pb_handler(self, path):
        def handler(address, *args):
            log.debug("Received OSC message for path: %s with args: %s", path, args)
            if self.main_loop and self.main_loop.is_running():
                asyncio.run_coroutine_threadsafe(
                    self.dglab_control.on_vrc_pb(path, *args),
                    self.main_loop,
                )
            else:
                log.error("Main event loop is not available")

        return handler

    def on_vrc_switch_handler(self, path, *args):
        if args and self.main_loop and self.main_loop.is_running():
            self.main_loop.call_soon_threadsafe(self.set_enabled, bool(args[0]))

    def set_enabled(self, enabled):
        self.dglab_control.enable = enabled
        self.sps_control.reset_inputs()
        if self.sps_control.enabled:
            self.osc.send_parameter("OGB_ENABLED", enabled)
        log.info("Dglab control enabled: %s", enabled)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--osc-only", action="store_true",
                        help="Diagnose VRChat OSC without connecting to DG-LAB")
    parser.add_argument("--debug", action="store_true", help="Show OSC values and detailed status logs")
    args = parser.parse_args()
    configure_logging(args.debug or args.osc_only)
    dglab = DglabVrc()
    try:
        asyncio.run(dglab.run(osc_only=args.osc_only))
    except KeyboardInterrupt:
        log.info("Shutting down DglabVrc...")
    finally:
        dglab.osc.close()
        log.info("DglabVrc has been shut down.")
