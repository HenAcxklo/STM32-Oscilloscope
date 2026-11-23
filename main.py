#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""STM32 串口示波器（PyQt5 + pyqtgraph）

下位机每帧数据格式（小端）::

    AA 55 | 256 × int16 | 5A A5        共 516 字节

- 每个 int16 是 STM32 内部 12 位 ADC 的原始码值；
- 电压 = 码值 × 3.3 / 4095；
- 程序默认把最近 N 帧叠加显示（类似余辉），N 可在顶部调整；
- 触发电平/边沿把每帧对齐到屏幕最左边，波形才会稳定；
- 横轴可选 1×/2×/3×，把连续 1/2/3 帧拼成一条更长的波形；
- 串口/波特率下拉框可编辑，支持手动输入 COMx 和任意 300~4000000 的波特率。

运行：``python main.py``，或双击同目录的 ``启动.bat``。
"""

from __future__ import annotations

import os
import sys
from collections import deque

# 必须在 import pyqtgraph 之前指定 Qt 绑定：
# 电脑上同时装了 PyQt5/PySide6 时，pyqtgraph 可能选错库导致崩溃。
os.environ.setdefault("PYQTGRAPH_QT_LIB", "PyQt5")

import struct
import threading
import time

import numpy as np
import pyqtgraph as pg
import serial
from PyQt5.QtCore import Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QIntValidator
from PyQt5.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

# ----------------------------- 用户配置 -----------------------------
DEFAULT_PORT = "COM5"                       # 启动时尝试连接的串口
PORT_CANDIDATES = ("COM3", "COM4", "COM5")  # 下拉框里的串口列表
DEFAULT_BAUD = 2000000                      # 启动时使用的波特率
BAUD_RATES = (                              # 下拉框里的波特率列表
    4800, 9600, 38400, 57600, 115200,
    230400, 460800, 921600, 2000000,
)
BAUD_MIN = 300                               # 手动输入波特率下限
BAUD_MAX = 4_000_000                         # 手动输入波特率上限

BUF_SIZE = 256               # 每帧采样点数，必须和固件一致
VOLTS_PER_LSB = 3.3 / 4095   # STM32 12 位 ADC，0~4095 对应 0~3.3 V
PLOT_INTERVAL_MS = 30        # 界面刷新周期，约 33 FPS

MAX_OVERLAY = 20             # 最多叠加 20 帧
DEFAULT_OVERLAY = 6          # 默认叠加 6 帧
AFTERGLOW_S = 0.05           # 余晖约 50 ms 内从亮到暗消失；想更短改小
MAX_HISTORY = MAX_OVERLAY * 3  # 3× 横轴 + 20 帧叠加要保留 60 帧原始数据

# 自动 Y 轴相关
Y_MIN, Y_MAX = -0.5, 3.5     # 自动缩放前的初始视野（V）
Y_MARGIN_RATIO = 0.10        # 上下各留当前峰峰值 10% 的余量
Y_MARGIN_MIN = 0.05          # 余量下限（V），防止小信号贴边
Y_MIN_SPAN = 0.2             # 纵轴最小跨度（V），防止噪声把视野压得太窄
Y_SMOOTH = 0.10              # 收缩平滑系数（0~1，越大收缩越快）
AXIS_LIMIT = 4.2             # 纵轴上限（略高于 STM32 3.3 V 量程）
# -------------------------------------------------------------------

FRAME_HEAD = b"\xAA\x55"
FRAME_TAIL = b"\x5A\xA5"
PAYLOAD_LEN = BUF_SIZE * 2
FRAME_LEN = PAYLOAD_LEN + len(FRAME_HEAD) + len(FRAME_TAIL)  # 516


class ScopeApp(QMainWindow):
    # 串口读线程掉线时，用信号把错误抛回 GUI 线程处理
    connection_lost = pyqtSignal(str)

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("STM32 示波器 — 未连接")
        self.resize(1240, 620)

        # ---- 线程共享数据 ----
        self.lock = threading.Lock()
        self.data_buffer = np.zeros(BUF_SIZE, dtype=np.float32)
        self.byte_buffer = bytearray()
        self.frames_received = 0
        self.history: deque[np.ndarray] = deque(maxlen=MAX_HISTORY)
        self.ext_history: deque[np.ndarray] = deque(maxlen=MAX_OVERLAY)
        self._last_ext_frames = 0
        self._x_scale = 0

        # ---- 串口连接状态（只在 GUI 线程里改） ----
        self.ser: serial.Serial | None = None
        self.thread: threading.Thread | None = None
        self._reader_stop: threading.Event | None = None
        self._closing = False

        self._build_ui()
        self.connection_lost.connect(self._on_connection_lost)

        # ---- 刷新定时器与帧率统计 ----
        self._fps_time = time.monotonic()
        self._fps_frames = 0
        self._fps = 0.0
        self._auto_last_count = 0
        self._overlay_last = 0
        self._y_lo, self._y_hi = float(Y_MIN), float(Y_MAX)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.update_plot)
        self.timer.start(PLOT_INTERVAL_MS)

        # 启动时自动尝试连接默认串口；失败只提示，不拦截界面
        self.connect_serial(user_initiated=False)

    # ------------------------------------------------------------------
    # 界面
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        central = QWidget(self)
        self.setCentralWidget(central)
        outer = QVBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        bar = QWidget()
        row = QHBoxLayout(bar)
        row.setContentsMargins(8, 6, 8, 6)
        row.setSpacing(6)

        row.addWidget(QLabel("串口"))
        self.port_combo = QComboBox()
        self.port_combo.setEditable(True)
        self.port_combo.setInsertPolicy(QComboBox.NoInsert)
        self.port_combo.addItems(PORT_CANDIDATES)
        self.port_combo.setEditText(DEFAULT_PORT)
        self.port_combo.lineEdit().setPlaceholderText("如 COM7 / /dev/ttyUSB0")
        self.port_combo.setMinimumWidth(100)
        self.port_combo.setToolTip("可直接输入串口号，例如 COM7；下拉框只是常用项")
        row.addWidget(self.port_combo)

        row.addSpacing(8)
        row.addWidget(QLabel("波特率"))
        self.baud_combo = QComboBox()
        self.baud_combo.setEditable(True)
        self.baud_combo.setInsertPolicy(QComboBox.NoInsert)
        for rate in BAUD_RATES:
            self.baud_combo.addItem(str(rate), rate)
        self.baud_combo.setEditText(str(DEFAULT_BAUD))
        self.baud_combo.lineEdit().setValidator(
            QIntValidator(BAUD_MIN, BAUD_MAX, self.baud_combo)
        )
        self.baud_combo.lineEdit().setPlaceholderText(f"{BAUD_MIN}~{BAUD_MAX}")
        self.baud_combo.setMinimumWidth(110)
        self.baud_combo.setToolTip(
            f"可直接输入波特率（{BAUD_MIN}~{BAUD_MAX}）；必须和下位机一致"
        )
        row.addWidget(self.baud_combo)

        self.connect_btn = QPushButton("连接")
        self.connect_btn.setMinimumWidth(72)
        self.connect_btn.clicked.connect(self.toggle_connection)
        row.addWidget(self.connect_btn)

        row.addSpacing(10)
        row.addWidget(QLabel("叠加"))
        self.overlay_spin = QSpinBox()
        self.overlay_spin.setRange(1, MAX_OVERLAY)
        self.overlay_spin.setValue(DEFAULT_OVERLAY)
        self.overlay_spin.setSuffix(" 帧")
        self.overlay_spin.setToolTip("同时显示最近 N 帧，旧帧逐渐变暗（余辉叠加）")
        row.addWidget(self.overlay_spin)

        row.addSpacing(10)
        row.addWidget(QLabel("横轴"))
        self.xscale_combo = QComboBox()
        self.xscale_combo.addItem("1×", 1)
        self.xscale_combo.addItem("2×", 2)
        self.xscale_combo.addItem("3×", 3)
        self.xscale_combo.setToolTip(
            "把连续 1/2/3 帧拼成一条更长的横轴；\n"
            "2×/3× 要等 2/3 帧才更新一次，会有一点延迟。"
        )
        row.addWidget(self.xscale_combo)

        row.addSpacing(10)
        self.trigger_enable = QCheckBox("触发")
        self.trigger_enable.setChecked(True)
        self.trigger_enable.setToolTip("按触发电平对齐每一帧，波形才会稳定")
        row.addWidget(self.trigger_enable)

        row.addWidget(QLabel("电平"))
        self.trigger_level = QDoubleSpinBox()
        self.trigger_level.setRange(0.0, 3.3)
        self.trigger_level.setDecimals(2)
        self.trigger_level.setSingleStep(0.05)
        self.trigger_level.setValue(1.65)
        self.trigger_level.setSuffix(" V")
        self.trigger_level.setFixedWidth(92)
        self.trigger_level.setToolTip("触发电平，一般设在信号中间；红色虚线就是它")
        row.addWidget(self.trigger_level)

        self.trigger_edge = QComboBox()
        self.trigger_edge.addItem("上升沿", "rising")
        self.trigger_edge.addItem("下降沿", "falling")
        self.trigger_edge.setToolTip("在触发电平的哪个方向对齐")
        row.addWidget(self.trigger_edge)

        self.trigger_state = QLabel("等待")
        self.trigger_state.setMinimumWidth(56)
        self.trigger_state.setStyleSheet("color: #9e9e9e;")
        row.addWidget(self.trigger_state)

        self.trigger_enable.toggled.connect(self.trigger_level.setEnabled)
        self.trigger_enable.toggled.connect(self.trigger_edge.setEnabled)

        row.addStretch(1)
        self.auto_y = QCheckBox("自动 Y 轴")
        self.auto_y.setChecked(True)
        self.auto_y.setToolTip("按当前叠加的所有帧的最小/最大值自动缩放纵轴")
        row.addWidget(self.auto_y)

        outer.addWidget(bar)

        self.plot_widget = pg.PlotWidget()
        outer.addWidget(self.plot_widget, 1)
        self.plot_widget.setBackground("k")
        self.plot_widget.showGrid(x=True, y=True)
        self.plot_widget.setLabel("left", "电压", units="V")
        self.plot_widget.setLabel("bottom", "采样点")
        self.plot_widget.setYRange(Y_MIN, Y_MAX)
        self.plot_widget.setXRange(0, BUF_SIZE, padding=0)

        # 一帧一个曲线；旧帧暗、细，新帧亮、粗
        self.curves: list[pg.PlotDataItem] = []
        for _ in range(MAX_OVERLAY):
            curve = self.plot_widget.plot(
                pen=pg.mkPen(pg.mkColor(255, 220, 0, 200), width=1)
            )
            curve.setVisible(False)
            self.curves.append(curve)

        # 触发电平线（红色虚线）
        self.trigger_line = pg.InfiniteLine(
            angle=0,
            movable=False,
            pen=pg.mkPen(pg.mkColor(255, 90, 90, 160), width=1, style=Qt.DashLine),
        )
        self.trigger_line.setZValue(10)
        self.plot_widget.addItem(self.trigger_line)
        self.trigger_line.setPos(1.65)

    def _set_connected_ui(self, connected: bool, port: str = "", baud: int = 0) -> None:
        self.connect_btn.setText("断开" if connected else "连接")
        self.port_combo.setEnabled(not connected)
        self.baud_combo.setEnabled(not connected)
        if connected:
            self.setWindowTitle(f"STM32 示波器 — {port} @ {baud} bps")
        else:
            self.setWindowTitle("STM32 示波器 — 未连接")

    # ------------------------------------------------------------------
    # 连接管理
    # ------------------------------------------------------------------
    def toggle_connection(self) -> None:
        if self.ser is None:
            self.connect_serial(user_initiated=True)
        else:
            self.disconnect_serial()

    def _reject_connection_input(self, message: str, user_initiated: bool) -> bool:
        """串口/波特率输入不合法时给出提示，不打开串口。"""
        self._set_connected_ui(False)
        if user_initiated:
            QMessageBox.warning(self, "串口参数错误", message)
        else:
            self.statusBar().showMessage(f"{message}修改后点「连接」")
        return False

    def connect_serial(self, user_initiated: bool = True) -> bool:
        """打开当前输入/选择的串口，并启动接收线程。"""
        if self.ser is not None:
            return True

        port = self.port_combo.currentText().strip()
        baud_text = self.baud_combo.currentText().strip()

        if not port:
            return self._reject_connection_input(
                "串口号不能为空，请先输入或选择串口（例如 COM7）。",
                user_initiated,
            )

        try:
            baud = int(baud_text)
        except ValueError:
            baud = 0

        if not (BAUD_MIN <= baud <= BAUD_MAX):
            return self._reject_connection_input(
                f"波特率必须是 {BAUD_MIN}~{BAUD_MAX} 之间的整数，"
                f"当前输入：{baud_text or '空'}。",
                user_initiated,
            )

        try:
            ser = serial.Serial(port, baud, timeout=0.01)
        except Exception as exc:  # noqa: BLE001 - 把原因展示给用户
            self.ser = None
            self._set_connected_ui(False)
            msg = f"{port} 打开失败：{exc}"
            print(msg)
            if user_initiated:
                QMessageBox.warning(
                    self,
                    "串口打开失败",
                    f"{msg}\n\n请检查：\n"
                    "1. 串口号是否正确（设备管理器里确认）；\n"
                    "2. 串口是否被串口助手等软件占用；\n"
                    "3. USB 转串口驱动是否安装。",
                )
            else:
                self.statusBar().showMessage(f"{msg}；请选择串口后点「连接」")
            return False

        with self.lock:
            self.ser = ser
            self.byte_buffer.clear()
            self.data_buffer = np.zeros(BUF_SIZE, dtype=np.float32)
            self.frames_received = 0
            self._auto_last_count = 0
            self.history.clear()

        self.ext_history.clear()
        self._last_ext_frames = 0
        self._x_scale = 0

        self._fps_time = time.monotonic()
        self._fps_frames = 0
        self._fps = 0.0

        self._reader_stop = threading.Event()
        self.thread = threading.Thread(
            target=self.serial_reader,
            args=(ser, self._reader_stop),
            name="serial-reader",
            daemon=True,
        )
        self.thread.start()

        self._set_connected_ui(True, port, baud)
        self.statusBar().showMessage(f"{port} @ {baud} bps 已连接，等待数据…")
        return True

    def disconnect_serial(self, update_status: bool = True) -> None:
        """停接收线程并关闭串口；画面保留最后一帧。"""
        stop, self._reader_stop = self._reader_stop, None
        if stop is not None:
            stop.set()

        ser, self.ser = self.ser, None
        if ser is not None:
            try:
                ser.close()
            except Exception:  # noqa: BLE001 - 关串口失败不影响界面
                pass

        thread, self.thread = self.thread, None
        if (
            thread is not None
            and thread.is_alive()
            and thread is not threading.current_thread()
        ):
            thread.join(timeout=0.4)

        self._set_connected_ui(False)
        self.trigger_state.setText("等待" if self.trigger_enable.isChecked() else "关闭")
        if update_status:
            self.statusBar().showMessage("未连接 — 选择串口和波特率后点「连接」")

    def _on_connection_lost(self, message: str) -> None:
        if self._closing or self.ser is None:
            return
        self.disconnect_serial(update_status=False)
        self.statusBar().showMessage(f"连接已断开：{message}  ——  点「连接」可重试")

    # ------------------------------------------------------------------
    # 接收线程
    # ------------------------------------------------------------------
    def serial_reader(self, ser: serial.Serial, stop: threading.Event) -> None:
        """后台线程：不停读串口 → 拼帧 → 校验 → 换算电压。"""
        while not stop.is_set():
            try:
                # 有多少读多少；没有数据时最多阻塞 timeout=10 ms
                data = ser.read(ser.in_waiting or 1)
            except Exception as exc:  # noqa: BLE001 - 拔线/关串口时会走到这里
                if not stop.is_set():
                    self.connection_lost.emit(str(exc))
                break

            if not data:
                continue

            with self.lock:
                self.byte_buffer.extend(data)
                frames = self._drain_frames()
                if frames:
                    # 全部按顺序塞进历史，界面只取最近 N 帧叠加
                    for frame in frames:
                        self.history.append(frame)
                    self.data_buffer = frames[-1]
                    self.frames_received += len(frames)

    def _drain_frames(self) -> list[np.ndarray]:
        """从 byte_buffer 中取出所有完整、校验通过的帧（调用方需持锁）。"""
        out: list[np.ndarray] = []
        buf = self.byte_buffer

        while True:
            head = buf.find(FRAME_HEAD)
            if head < 0:
                # 没有帧头：只保留最后 1 字节，防止 AA 55 正好被拆在两次读取之间
                if len(buf) > 1:
                    del buf[:-1]
                break

            if head > 0:
                del buf[:head]  # 丢掉帧头前的杂数据

            if len(buf) < FRAME_LEN:
                break  # 半帧，等下一批数据

            if buf[FRAME_LEN - 2:FRAME_LEN] == FRAME_TAIL:
                payload = bytes(buf[2:FRAME_LEN - 2])
                # '<256h'：小端、256 个有符号 16 位整数
                raw = np.asarray(struct.unpack("<256h", payload), dtype=np.float32)
                out.append(raw * VOLTS_PER_LSB)
                del buf[:FRAME_LEN]
            else:
                # 帧尾不对，说明是假帧头：跳过 1 字节重新同步
                del buf[0]

        return out

    # ------------------------------------------------------------------
    # 界面刷新
    # ------------------------------------------------------------------
    def update_plot(self) -> None:
        with self.lock:
            data = self.data_buffer.copy()
            frames = self.frames_received
            hist = list(self.history)

        m = int(self.xscale_combo.currentData() or 1)
        if m != self._x_scale:
            self._x_scale = m
            self.plot_widget.setXRange(0, BUF_SIZE * m, padding=0)
            self.plot_widget.setLabel("bottom", f"采样点（{BUF_SIZE * m}）")
            self.ext_history.clear()
            self._rebuild_ext(hist, m, frames)
            self._auto_last_count = -1          # 强制重算一次 Y 范围

        if m == 1:
            all_traces = hist
        else:
            self._append_ext(hist, frames, m)
            all_traces = list(self.ext_history)

        n = min(self.overlay_spin.value(), len(all_traces))
        raw_visible = all_traces[-n:] if n else []

        # 每帧单独做触发对齐，叠加起来才会稳定。
        # 触发点放最左边：帧内没有真正的 pre-trigger 数据，放中间会用帧尾硬绕回来，
        # 反而在左半边出现接缝/跳变。
        trigger_pos = 0
        visible = []
        triggered_any = False
        for trace in raw_visible:
            aligned, ok = self._align_trigger(trace, trigger_pos)
            visible.append(aligned)
            triggered_any |= ok

        shown: list[np.ndarray] = []
        for i, curve in enumerate(self.curves):
            if i < n:
                # 越新的帧越亮；2×/3× 时每条波形间隔 m 帧
                age = (n - 1 - i) * m / max(self._fps, 1.0)
                frac = 1.0 - age / AFTERGLOW_S
                if frac <= 0.05:
                    curve.setVisible(False)
                    continue
                alpha = int(45 + 210 * frac)
                width = 2 if i == n - 1 else 1
                curve.setPen(pg.mkPen(pg.mkColor(255, 220, 0, alpha), width=width))
                curve.setData(visible[i])
                curve.setVisible(True)
                shown.append(visible[i])
            else:
                curve.setVisible(False)

        if (
            shown
            and self.auto_y.isChecked()
            and (frames != self._auto_last_count or n != self._overlay_last)
        ):
            self._auto_last_count = frames
            self._overlay_last = n
            combined = np.concatenate(shown) if len(shown) > 1 else shown[0]
            self._auto_range(combined)

        # 触发电平线
        self.trigger_line.setPos(float(self.trigger_level.value()))
        self.trigger_line.setVisible(self.trigger_enable.isChecked())

        if self.trigger_enable.isChecked():
            self.trigger_state.setText("已触发" if triggered_any else "等待")
            self.trigger_state.setStyleSheet(
                "color: #66bb6a;" if triggered_any else "color: #ffa726;"
            )
        else:
            self.trigger_state.setText("关闭")
            self.trigger_state.setStyleSheet("color: #9e9e9e;")

        if self.ser is None or frames == 0:
            return  # 未连接/还没收到帧时，保留连接提示

        now = time.monotonic()
        elapsed = now - self._fps_time
        if elapsed >= 0.5:  # 每 0.5 s 统计一次帧率，避免数字乱跳
            self._fps = (frames - self._fps_frames) / elapsed
            self._fps_time = now
            self._fps_frames = frames

        latest = hist[-1] if hist else data
        v_now = float(latest[-1])
        v_pp = float(latest.max() - latest.min())
        self.statusBar().showMessage(
            f"{self.port_combo.currentText()} @ {self.baud_combo.currentText()} bps    "
            f"横轴 {m}×    帧率 {self._fps:5.1f} fps    "
            f"最新 {v_now:+.3f} V    峰峰值 {v_pp:.3f} V"
        )

    def _align_trigger(
        self, trace: np.ndarray, trigger_pos: int
    ) -> tuple[np.ndarray, bool]:
        """把触发电平点滚到 trigger_pos，返回对齐后的波形和是否触发。"""
        if not self.trigger_enable.isChecked() or trace.size < 3:
            return trace, False

        level = float(self.trigger_level.value())
        span = float(trace.max() - trace.min())
        if span < 0.01:
            return trace, False

        hys = max(0.005, span * 0.02)
        rising = self.trigger_edge.currentData() != "falling"

        if rising:
            armed = np.maximum.accumulate(trace < (level - hys))[:-1]
            cross = (trace[1:] > (level + hys)) & armed
        else:
            armed = np.maximum.accumulate(trace > (level + hys))[:-1]
            cross = (trace[1:] < (level - hys)) & armed

        idx = np.flatnonzero(cross)
        if idx.size == 0:
            return trace, False

        i = int(idx[0]) + 1
        y0 = float(trace[i - 1])
        y1 = float(trace[i])
        denom = y1 - y0
        frac = (level - y0) / denom if abs(denom) > 1e-9 else 0.0
        pos = (i - 1) + frac
        shift = trigger_pos - pos

        n = trace.size
        x_new = np.arange(n) - shift
        # 触发点放左边，后面没有的样本用 NaN 补，不再循环绕回，
        # 否则帧尾会硬接到帧头，出现竖直接缝。
        aligned = np.interp(x_new, np.arange(n), trace, left=np.nan, right=np.nan)
        return aligned, True

    def _rebuild_ext(self, hist: list[np.ndarray], m: int, frames: int) -> None:
        """切换 1×/2×/3× 时，用历史帧立刻拼出可见的延长帧。"""
        self.ext_history.clear()
        self._last_ext_frames = frames
        if m <= 1 or len(hist) < m:
            return

        n_chunks = min(len(hist) // m, MAX_OVERLAY)
        chunk = hist[-n_chunks * m:]
        for k in range(n_chunks):
            self.ext_history.append(np.concatenate(chunk[k * m:(k + 1) * m]))

    def _append_ext(self, hist: list[np.ndarray], frames: int, m: int) -> None:
        """每收满 m 帧，追加一条 m×256 的延长波形（会带来 m 帧延迟）。"""
        if len(hist) < m:
            return

        new_groups = (frames - self._last_ext_frames) // m
        if new_groups <= 0:
            return

        take = min(new_groups, MAX_OVERLAY)
        if len(hist) < take * m:
            take = len(hist) // m
        if take <= 0:
            return

        chunk = hist[-take * m:]
        for k in range(take):
            self.ext_history.append(np.concatenate(chunk[k * m:(k + 1) * m]))
        self._last_ext_frames = frames

    def _auto_range(self, data: np.ndarray) -> None:
        """按当前帧最小/最大值缩放纵轴：放大立即生效，缩小平滑过渡。"""
        finite = data[np.isfinite(data)]
        if finite.size == 0:
            return
        lo = float(finite.min())
        hi = float(finite.max())
        if hi - lo < 1e-6:  # 完全平直时给一个可见的窗口
            lo -= 0.05
            hi += 0.05

        margin = max((hi - lo) * Y_MARGIN_RATIO, Y_MARGIN_MIN)
        lo = max(lo - margin, -AXIS_LIMIT)
        hi = min(hi + margin, AXIS_LIMIT)

        new_lo = self._y_lo + (lo - self._y_lo) * Y_SMOOTH
        new_hi = self._y_hi + (hi - self._y_hi) * Y_SMOOTH
        # 保证当前帧一定在视野里（只会立即扩张，不会立即收缩）
        new_lo = min(new_lo, lo)
        new_hi = max(new_hi, hi)

        if new_hi - new_lo < Y_MIN_SPAN:
            mid = (new_hi + new_lo) / 2
            new_lo = mid - Y_MIN_SPAN / 2
            new_hi = mid + Y_MIN_SPAN / 2

        if abs(new_lo - self._y_lo) > 0.002 or abs(new_hi - self._y_hi) > 0.002:
            self._y_lo, self._y_hi = new_lo, new_hi
            self.plot_widget.setYRange(new_lo, new_hi, padding=0)

    # ------------------------------------------------------------------
    def closeEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        self._closing = True
        self.timer.stop()
        self.disconnect_serial(update_status=False)
        event.accept()


def main() -> None:
    # 高分屏缩放（必须在 QApplication 创建之前设置）
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)

    pg.setConfigOptions(antialias=False)

    app = QApplication(sys.argv)
    app.setApplicationName("STM32 示波器")

    window = ScopeApp()
    window.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
