import asyncio
import collections
import logging
import os
import sys
import tempfile
import threading
import time
from pathlib import Path


if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")

import dearpygui.dearpygui as dpg
from dglabv3 import Channel

import main

log = logging.getLogger("gui")
LOG_LINES = 300
FONT_CANDIDATES = (
    r"C:\Windows\Fonts\msjh.ttc",
    r"C:\Windows\Fonts\msyh.ttc",
)


BODY_RECTS = {
    "chest": (80, 60, 140, 120),
    "abdomen": (82, 124, 138, 178),
    "right_arm": (48, 62, 74, 128),
    "right_arm_1": (50, 132, 72, 198),
    "left_arm": (146, 62, 172, 128),
    "left_arm_1": (148, 132, 170, 198),
    "right_leg": (80, 182, 107, 262),
    "right_leg_1": (82, 266, 105, 338),
    "left_leg": (113, 182, 140, 262),
    "left_leg_1": (115, 266, 138, 338),
}
LIMB_LABELS = {
    "right_arm": "右手",
    "left_arm": "左手",
    "right_leg": "右腳",
    "left_leg": "左腳",
}
PART_LABELS = {
    "right_arm": "右手 right_arm",
    "right_arm_1": "右前臂 right_arm_1",
    "left_arm": "左手 left_arm",
    "left_arm_1": "左前臂 left_arm_1",
    "right_leg": "右腳 right_leg",
    "right_leg_1": "右小腿 right_leg_1",
    "left_leg": "左腳 left_leg",
    "left_leg_1": "左小腿 left_leg_1",
    "chest": "胸 chest",
    "abdomen": "腹 abdomen",
}
CHANNEL_COLORS = {"A": (255, 160, 60), "B": (80, 160, 255), None: (90, 90, 90)}


class GuiLogHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = collections.deque()
        self._lines_lock = threading.Lock()
        self.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)s %(name)s: %(message)s", "%H:%M:%S"
            )
        )

    def emit(self, record):
        try:
            line = self.format(record)
        except Exception:
            return
        with self._lines_lock:
            self.lines.append((record.levelno, line))

    def drain(self):
        with self._lines_lock:
            lines = list(self.lines)
            self.lines.clear()
        return lines


class Backend:
    """Owns the asyncio loop running DglabVrc on a background thread."""

    def __init__(self, log_handler):
        self.log_handler = log_handler
        self.thread = None
        self.loop = None
        self.task = None
        self.app = None
        self.qr_png = None
        self.qr_version = 0
        self.error = None
        self.osc_only = False

    @property
    def running(self):
        return self.thread is not None and self.thread.is_alive()

    def start(self, osc_only, debug):
        if self.running:
            return
        self.error = None
        self.qr_png = None
        self.osc_only = osc_only
        main.configure_logging(debug or osc_only)
        logging.getLogger().addHandler(self.log_handler)
        self.thread = threading.Thread(target=self._thread_main, daemon=True)
        self.thread.start()

    def stop(self):
        if self.loop is not None and self.task is not None:
            self.loop.call_soon_threadsafe(self.task.cancel)

    def join(self, timeout=5):
        if self.thread is not None:
            self.thread.join(timeout)

    def set_enabled(self, enabled):
        if self.loop is not None and self.app is not None and self.loop.is_running():
            self.loop.call_soon_threadsafe(self.app.set_enabled, enabled)

    def _on_qrcode(self, png):
        self.qr_png = png
        self.qr_version += 1

    def _thread_main(self):
        try:
            main.config = main.load_config()
            self.app = main.DglabVrc()
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
            log.exception("Startup failed")
            self.app = None
            return
        self.app.on_qrcode = self._on_qrcode
        try:
            asyncio.run(self._run())
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
            log.exception("DglabVrc stopped with an error")
        finally:
            self.app.osc.close()
            self.loop = None
            self.task = None
            log.info("DglabVrc has been shut down.")

    async def _run(self):
        self.loop = asyncio.get_running_loop()
        self.task = asyncio.current_task()
        try:
            await self.app.run(osc_only=self.osc_only)
        except asyncio.CancelledError:
            pass


class Gui:
    def __init__(self):
        self.log_handler = GuiLogHandler()
        self.backend = Backend(self.log_handler)
        self.log_items = collections.deque()
        self.qr_shown = 0
        self.qr_dismissed = False
        self.qr_texture = None
        self.scroll_pending = False
        logging.getLogger().addHandler(self.log_handler)

    def setup_font(self):
        for path in FONT_CANDIDATES:
            if Path(path).exists():
                with dpg.font_registry():
                    font = dpg.add_font(path, 18)
                dpg.bind_font(font)
                return

    def setup_theme(self):
        with dpg.theme() as theme:
            with dpg.theme_component(dpg.mvAll):
                dpg.add_theme_style(dpg.mvStyleVar_FrameRounding, 4)
                dpg.add_theme_style(dpg.mvStyleVar_WindowPadding, 12, 12)
                dpg.add_theme_style(dpg.mvStyleVar_ItemSpacing, 8, 6)
        dpg.bind_theme(theme)
        with dpg.theme(tag="theme_warn"):
            with dpg.theme_component(dpg.mvText):
                dpg.add_theme_color(dpg.mvThemeCol_Text, (255, 200, 80))
        with dpg.theme(tag="theme_error"):
            with dpg.theme_component(dpg.mvText):
                dpg.add_theme_color(dpg.mvThemeCol_Text, (255, 100, 100))

    def build(self):
        with dpg.texture_registry(tag="textures"):
            pass
        with dpg.window(tag="main"):
            with dpg.group(horizontal=True):
                dpg.add_button(
                    label="啟動", tag="btn_start", width=80, callback=self.on_start
                )
                dpg.add_button(
                    label="停止",
                    tag="btn_stop",
                    width=80,
                    callback=self.on_stop,
                    enabled=False,
                )
                dpg.add_checkbox(label="僅 OSC 診斷", tag="opt_osc_only")
                dpg.add_checkbox(label="除錯日誌", tag="opt_debug")
                dpg.add_button(label="開啟設定檔", callback=self.on_open_config)
            dpg.add_separator()

            with dpg.group(horizontal=True):
                with dpg.child_window(width=360, height=490, border=True):
                    dpg.add_text("狀態")
                    with dpg.table(header_row=False, borders_innerH=False):
                        dpg.add_table_column(width_fixed=True, init_width_or_weight=110)
                        dpg.add_table_column()
                        for key, label in (
                            ("st_state", "執行狀態"),
                            ("st_osc", "OSC 封包"),
                            ("st_last", "最後封包"),
                            ("st_app", "DG-LAB App"),
                            ("st_devices", "裝置"),
                        ):
                            with dpg.table_row():
                                dpg.add_text(label)
                                dpg.add_text("-", tag=key, wrap=220)
                    dpg.add_spacer(height=4)
                    dpg.add_checkbox(
                        label="輸出啟用",
                        tag="opt_enable",
                        default_value=True,
                        callback=lambda s, v: self.backend.set_enabled(v),
                    )
                    dpg.add_spacer(height=4)
                    dpg.add_text("SPS")
                    for key, label in (
                        ("bar_depth", "深度"),
                        ("bar_a", "A"),
                        ("bar_b", "B"),
                    ):
                        with dpg.group(horizontal=True):
                            dpg.add_text(label)
                            dpg.add_progress_bar(tag=key, width=-1, overlay="0%")
                with dpg.child_window(height=490, border=True, tag="body_panel"):
                    dpg.add_text("身體部位對應")
                    self.mapping = main.channel_map(main.load_config())
                    self.selected = None
                    with dpg.group(horizontal=True):
                        with dpg.drawlist(width=220, height=345, tag="body_draw"):
                            dpg.draw_circle(
                                (110, 30), 22, color=(150, 150, 150), thickness=2
                            )
                            for part, rect in BODY_RECTS.items():
                                dpg.draw_rectangle(
                                    rect[:2],
                                    rect[2:],
                                    tag=f"shape_{part}",
                                    thickness=2,
                                    rounding=6,
                                )
                        with dpg.group():
                            dpg.add_text("整肢設定")
                            with dpg.group(horizontal=True):
                                for limb, label in LIMB_LABELS.items():
                                    with dpg.group():
                                        dpg.add_text(label)
                                        dpg.add_combo(
                                            ("關閉", "A", "B", "自訂"),
                                            width=66,
                                            tag=f"limb_{limb}",
                                            user_data=limb,
                                            callback=self.on_limb_change,
                                        )
                            dpg.add_spacer(height=6)
                            dpg.add_text("個別部位")
                            with dpg.table(header_row=False):
                                dpg.add_table_column(
                                    width_fixed=True, init_width_or_weight=160
                                )
                                dpg.add_table_column()
                                for part in main.BODY_PARTS:
                                    with dpg.table_row():
                                        dpg.add_text(
                                            PART_LABELS[part], tag=f"lbl_{part}"
                                        )
                                        dpg.add_combo(
                                            ("關閉", "A", "B"),
                                            width=80,
                                            tag=f"map_{part}",
                                            user_data=part,
                                            callback=self.on_part_change,
                                        )
                    with dpg.handler_registry():
                        dpg.add_mouse_click_handler(callback=self.on_body_click)
                    self.refresh_mapping()

            dpg.add_text("日誌")
            dpg.add_child_window(
                tag="log_panel", height=-1, border=True, horizontal_scrollbar=True
            )

        with dpg.window(
            tag="qr_window",
            label="DG-LAB 配對",
            modal=True,
            show=False,
            no_close=True,
            no_resize=True,
            no_collapse=True,
            width=340,
            height=470,
        ):
            dpg.add_text("", tag="qr_hint", wrap=300)
            dpg.add_button(
                label="關閉並停止", tag="qr_close", width=-1, callback=self.on_qr_close
            )

    def on_start(self):
        self.backend.start(dpg.get_value("opt_osc_only"), dpg.get_value("opt_debug"))

    def on_qr_close(self):
        self.qr_dismissed = True
        dpg.configure_item("qr_window", show=False)
        self.backend.stop()

    def on_stop(self):
        self.backend.stop()

    @staticmethod
    def _channel(value):
        return value if value in ("A", "B") else None

    def on_part_change(self, sender, value, part):
        self.selected = part
        self.mapping[part] = self._channel(value)
        self.save_mapping()

    def on_limb_change(self, sender, value, limb):
        if value == "自訂":
            self.refresh_mapping()
            return
        for part in main.LIMB_GROUPS[limb]:
            self.mapping[part] = self._channel(value)
        self.save_mapping()

    def on_body_click(self):
        if self.backend.running or not dpg.is_item_hovered("body_draw"):
            return
        x, y = dpg.get_drawing_mouse_pos()
        for part, (x1, y1, x2, y2) in BODY_RECTS.items():
            if x1 <= x <= x2 and y1 <= y <= y2:
                self.selected = part
                order = (None, "A", "B")
                self.mapping[part] = order[(order.index(self.mapping[part]) + 1) % 3]
                self.save_mapping()
                return

    def save_mapping(self):
        try:
            main.save_channel_map(self.mapping)
        except Exception:
            log.exception("Could not save config")
        else:
            log.info("Saved body part mapping; restart to apply")
        self.refresh_mapping()

    def refresh_mapping(self):
        for part in main.BODY_PARTS:
            channel = self.mapping[part]
            dpg.set_value(f"map_{part}", channel or "關閉")
            dpg.configure_item(
                f"shape_{part}",
                color=(255, 255, 255) if part == self.selected else (30, 30, 30),
                fill=CHANNEL_COLORS[channel] + (200,),
            )
        for limb, parts in main.LIMB_GROUPS.items():
            values = {self.mapping[p] for p in parts}
            dpg.set_value(
                f"limb_{limb}", (values.pop() or "關閉") if len(values) == 1 else "自訂"
            )

    def on_open_config(self):
        path = main.config_path()
        if not path.exists():
            path.write_text(main.DEFAULT_CONFIG, encoding="utf-8")
        os.startfile(path)

    def update(self):
        backend = self.backend
        running = backend.running
        app = backend.app if running else None
        dpg.configure_item("btn_start", enabled=not running)
        dpg.configure_item("btn_stop", enabled=running)
        dpg.configure_item("opt_osc_only", enabled=not running)
        dpg.configure_item("opt_debug", enabled=not running)
        for part in main.BODY_PARTS:
            dpg.configure_item(f"map_{part}", enabled=not running)
        for limb in main.LIMB_GROUPS:
            dpg.configure_item(f"limb_{limb}", enabled=not running)

        if running:
            state = "執行中（僅 OSC）" if backend.osc_only else "執行中"
        elif backend.error:
            state = f"錯誤：{backend.error}"
        else:
            state = "已停止"
        dpg.set_value("st_state", state)

        linked = False
        if app is not None:
            osc = app.osc
            dpg.set_value("st_osc", f"{osc.received}（OGB {osc.ogb_received}）")
            dpg.set_value(
                "st_last",
                "尚未收到"
                if osc.last_received is None
                else f"{time.monotonic() - osc.last_received:.1f} 秒前",
            )
            if backend.osc_only:
                dpg.set_value("st_app", "未使用")
                dpg.set_value("st_devices", "-")
            else:
                linked = app.dglab.is_linked_to_app()
                dpg.set_value("st_app", "已連線" if linked else "等待掃描")
                devices = [
                    f"{d.type}{'' if d.has_device else '（未連接）'}"
                    for d in app.dglab.devices
                ]
                dpg.set_value("st_devices", "、".join(devices) or "無")
            if dpg.get_value("opt_enable") != app.dglab_control.enable:
                dpg.set_value("opt_enable", app.dglab_control.enable)
            sps = app.sps_control
            self.set_bar("bar_depth", sps.last_depth)
            self.set_bar("bar_a", sps.last_levels[Channel.A])
            self.set_bar("bar_b", sps.last_levels[Channel.B])
        else:
            for key in ("st_osc", "st_last", "st_app", "st_devices"):
                dpg.set_value(key, "-")
            for key in ("bar_depth", "bar_a", "bar_b"):
                self.set_bar(key, 0.0)

        self.update_qr(running, linked)
        self.update_log()

    @staticmethod
    def set_bar(tag, value):
        dpg.set_value(tag, value)
        dpg.configure_item(tag, overlay=f"{value * 100:.0f}%")

    def update_qr(self, running, linked):
        backend = self.backend
        if backend.qr_version != self.qr_shown and backend.qr_png is not None:
            self.qr_shown = backend.qr_version
            self.qr_dismissed = False
            self.clear_qr()
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
                f.write(backend.qr_png)
                path = f.name
            try:
                width, height, _, data = dpg.load_image(path)
            finally:
                os.unlink(path)
            self.qr_texture = dpg.add_static_texture(
                width, height, data, parent="textures"
            )
            size = 280
            dpg.add_image(
                self.qr_texture,
                width=size,
                height=size,
                tag="qr_image",
                parent="qr_window",
            )
        show_qr = running and not linked and self.qr_texture is not None
        if self.qr_texture is not None and not show_qr:
            self.clear_qr()
        if not running:
            hint = "按下「啟動」後，用 DG-LAB App 掃描 QR Code"
        elif backend.osc_only:
            hint = "僅 OSC 診斷模式，不連線 DG-LAB"
        elif linked:
            hint = "App 已連線"
        elif show_qr:
            hint = "用 DG-LAB App 掃描 QR Code"
        else:
            hint = "正在連線 DG-LAB 伺服器…"
        dpg.set_value("qr_hint", hint)

        if show_qr and not self.qr_dismissed:
            if not dpg.is_item_shown("qr_window"):
                vw, vh = (
                    dpg.get_viewport_client_width(),
                    dpg.get_viewport_client_height(),
                )
                dpg.set_item_pos(
                    "qr_window", ((vw - 340) // 2, max(0, (vh - 470) // 2))
                )
                dpg.configure_item("qr_window", show=True)
        elif dpg.is_item_shown("qr_window"):
            dpg.configure_item("qr_window", show=False)

    def clear_qr(self):
        if dpg.does_item_exist("qr_image"):
            dpg.delete_item("qr_image")
        if self.qr_texture is not None:
            dpg.delete_item(self.qr_texture)
            self.qr_texture = None

    def update_log(self):
        panel = "log_panel"
        if self.scroll_pending:
            dpg.set_y_scroll(panel, dpg.get_y_scroll_max(panel))
            self.scroll_pending = False
        lines = self.log_handler.drain()
        if not lines:
            return
        at_bottom = dpg.get_y_scroll(panel) >= dpg.get_y_scroll_max(panel) - 4
        for level, line in lines:
            item = dpg.add_text(line, parent=panel)
            if level >= logging.ERROR:
                dpg.bind_item_theme(item, "theme_error")
            elif level >= logging.WARNING:
                dpg.bind_item_theme(item, "theme_warn")
            self.log_items.append(item)
        while len(self.log_items) > LOG_LINES:
            dpg.delete_item(self.log_items.popleft())
        self.scroll_pending = at_bottom

    def run(self):
        dpg.create_context()
        self.setup_font()
        self.setup_theme()
        self.build()
        dpg.create_viewport(
            title="DG-LAB VRC", width=1000, height=900, min_width=640, min_height=480
        )
        dpg.setup_dearpygui()
        dpg.show_viewport()
        dpg.set_primary_window("main", True)
        log.info("Config: %s", main.config_path())
        try:
            while dpg.is_dearpygui_running():
                self.update()
                dpg.render_dearpygui_frame()
        finally:
            self.backend.stop()
            self.backend.join()
            dpg.destroy_context()


if __name__ == "__main__":
    Gui().run()
