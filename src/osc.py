from queue import Queue
import asyncio
import threading
import time
from pythonosc import udp_client, dispatcher, osc_server


class OSC:
    def __init__(self):
        self.client = udp_client.SimpleUDPClient("127.0.0.1", 9000)
        self.running = True
        self.message_queue = Queue()
        self.loop = asyncio.new_event_loop()
        self.worker_thread = threading.Thread(
            target=self._process_messages, daemon=True
        )
        self.dispatchers = dispatcher.Dispatcher()
        self.server = None
        self.server_thread = None
        self.worker_thread.start()
        self.start_server()

    def start_server(self):
        """啟動 OSC 服務器來接收來自 VRChat 的訊息"""

        def run_server():
            try:
                self.server = osc_server.BlockingOSCUDPServer(
                    ("127.0.0.1", 9001), self.dispatchers
                )
                print("OSC Server started on 127.0.0.1:9001")
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
            print(f"Message sent: {message}")
        except Exception as e:
            print(f"Error sending message: {e}")

    def register_dispatcher(self, path: str, callback):
        """註冊一個新的 OSC 分派器"""
        self.dispatchers.map(path, callback)

    def send_message(self, message: str):
        """將消息添加到隊列中"""
        self.message_queue.put(message)

    def close(self):
        """關閉 OSC 客戶端和服務器"""
        self.running = False

        if self.server:
            self.server.shutdown()

        if self.worker_thread and self.worker_thread.is_alive():
            self.worker_thread.join(timeout=1.0)

        if self.server_thread and self.server_thread.is_alive():
            self.server_thread.join(timeout=1.0)

        try:
            if self.loop and self.loop.is_running():
                self.loop.call_soon_threadsafe(self.loop.stop)
        except:  # noqa: E722
            pass
