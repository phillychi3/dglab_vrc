from queue import Queue
import asyncio
import threading
import time
import logging
from pythonosc import udp_client, dispatcher, osc_server

log = logging.getLogger(__name__)


class OSC:
    def __init__(self, listen_port=9001):
        self.received = 0
        self.ogb_received = 0
        self.last_received = None
        self._logged_addresses = {}
        self.running = True
        self.message_queue = Queue()
        self.worker_thread = threading.Thread(
            target=self._process_messages, daemon=True
        )
        self.dispatchers = dispatcher.Dispatcher()
        self.dispatchers.map("/*", self._observe_message)
        # Bind synchronously so an occupied port stops startup visibly.
        self.server = osc_server.BlockingOSCUDPServer(
            ("127.0.0.1", listen_port),
            self.dispatchers,
        )
        self.client = udp_client.SimpleUDPClient("127.0.0.1", 9000)
        self.loop = asyncio.new_event_loop()
        self.server_thread = None
        self.worker_thread.start()
        self.start_server()

    def _observe_message(self, address, *args):
        self.received += 1
        self.last_received = time.monotonic()
        if self.received == 1:
            log.info("OSC first message received: %s", address)
        if "OGB" in address:
            self.ogb_received += 1
            if log.isEnabledFor(logging.DEBUG):
                now = time.monotonic()
                if now - self._logged_addresses.get(address, -float("inf")) >= 1:
                    log.debug("OSC OGB received: %s %s", address, args)
                    self._logged_addresses[address] = now

    def report_status(self):
        if log.isEnabledFor(logging.DEBUG):
            age = (
                None
                if self.last_received is None
                else round(time.monotonic() - self.last_received, 1)
            )
            log.debug(
                "OSC status: received=%s OGB=%s last_packet_seconds_ago=%s",
                self.received,
                self.ogb_received,
                age,
            )

    def start_server(self):
        """啟動 OSC 服務器來接收來自 VRChat 的訊息"""

        def run_server():
            try:
                log.info("OSC listening on %s:%s", *self.server.server_address)
                self.server.serve_forever()
            except Exception as e:
                print(f"Error starting OSC server: {e}")

        self.server_thread = threading.Thread(target=run_server, daemon=True)
        self.server_thread.start()

    def _process_messages(self):
        asyncio.set_event_loop(self.loop)
        while self.running:
            try:
                if not self.message_queue.empty():
                    message = self.message_queue.get()
                    self._send_message(message)
                time.sleep(0.01)
            except Exception as e:
                print(f"Error processing message: {e}")

    def _send_message(self, message: str):
        """發送消息到 VRChat"""
        try:
            self.client.send_message("/chatbox/input", [message, True])
            log.debug("Message sent: %s", message)
        except Exception as e:
            print(f"Error sending message: {e}")

    def register_dispatcher(self, path: str, callback):
        """註冊一個新的 OSC 分派器"""
        self.dispatchers.map(path, callback)

    def send_message(self, message: str):
        """將消息添加到隊列中"""
        self.message_queue.put(message)

    def send_parameter(self, name, value):
        self.client.send_message("/avatar/parameters/" + name, value)

    def close(self):
        """關閉 OSC 客戶端和服務器"""
        self.running = False

        if self.server:
            self.server.shutdown()

        if self.worker_thread and self.worker_thread.is_alive():
            self.worker_thread.join(timeout=1.0)

        if self.server_thread and self.server_thread.is_alive():
            self.server_thread.join(timeout=1.0)
        if self.server:
            self.server.server_close()

        try:
            if self.loop and self.loop.is_running():
                self.loop.call_soon_threadsafe(self.loop.stop)
            elif self.loop and not self.loop.is_closed():
                self.loop.close()
        except:  # noqa: E722
            pass
        # python-osc 1.9.3 does not expose a public client close method.
        self.client._sock.close()
