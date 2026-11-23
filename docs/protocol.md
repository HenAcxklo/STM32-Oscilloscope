# Serial Protocol — STM32 Serial Oscilloscope

**Protocol version:** 1 (frozen)  
**Status:** stable since firmware/host v1.0; the 516-byte frame layout has not changed.  
**Transport:** UART, 8 data bits, no parity, 1 stop bit (8-N-1), no flow control.  
**Default baud:** 2,000,000 bps.  
**Default frame rate:** ~19–330 fps, depending on the PA1 potentiometer (see §4).

This document is the wire contract between the STM32 acquisition firmware and the PC host. Everything else (auto-Y, persistence, trigger, 1×/2×/3×) is host-side display logic.

---

## 1. Frame format

Every frame is exactly **516 bytes**:

| Offset | Size | Field | Description |
| ---: | ---: | --- | --- |
| 0 | 2 | Header | `0xAA 0x55` |
| 2 | 512 | Payload | 256 × `int16`, **little-endian**, ADC codes |
| 514 | 2 | Tail | `0x5A 0xA5` |

Total: `2 + 256 × 2 + 2 = 516` bytes.

```
+--------+----------------------------------------+--------+
| AA 55  |  sample[0] ... sample[255]             | 5A A5  |
| 2 B    |  256 × int16 LE = 512 B                | 2 B    |
+--------+----------------------------------------+--------+
```

### 1.1 Sample values

- Each sample is a signed 16-bit integer in little-endian order.
- In the current firmware the values are STM32 12-bit ADC codes: **0…4095**.
- Values outside 0…4095 are not produced by the reference firmware; the host does not clamp them.

### 1.2 Voltage conversion

```
V = code × 3.3 / 4095
```

This assumes VDDA = 3.3 V. If the board’s actual VDDA differs, scale the coefficient accordingly.

> **Earlier ADS1115 build:** the same 516-byte frame was used, but the coefficient was `0.000125` V/LSB (±4.096 V PGA). The current STM32 internal-ADC firmware uses `3.3 / 4095`. Do not mix the two coefficients; check which firmware is running.

> **中文**：每帧 516 字节；帧头 `AA 55`，帧尾 `5A A5`；中间 256 个 **小端 int16**，当前固件输出 0–4095 的 12 位 ADC 码值，电压按 `码值 × 3.3 / 4095` 换算。早期 ADS1115 版本系数是 `0.000125`，帧格式相同但系数不同。

---

## 2. Byte order and framing rules

- Multi-byte samples are **little-endian** (`struct.unpack("<256h", ...)` in Python).
- There is **no CRC**, no frame counter, no timestamp and no escaping.
- A frame is valid only if both the header and the tail match.
- The receiver must scan for `AA 55`, collect 516 bytes, then check the last two bytes for `5A A5`.

### 2.1 Resynchronisation

If the tail does not match:

1. Drop **one byte** (not the whole buffer);
2. Search for the next `AA 55`;
3. Repeat.

This handles a false `AA 55` inside payload data and a frame that was truncated by a UART glitch.

### 2.2 Recommended parser (Python)

```python
import struct

HEAD = b"\xAA\x55"
TAIL = b"\x5A\xA5"
N = 256
FRAME_LEN = 2 + N * 2 + 2  # 516

buf = bytearray()

def feed(data: bytes):
    """Feed arbitrary UART bytes; return all complete frames."""
    buf.extend(data)
    frames = []

    while True:
        i = buf.find(HEAD)
        if i < 0:
            # Keep the last byte: it may be the first half of a split header.
            if buf:
                buf[:] = buf[-1:]
            break
        if i:
            del buf[:i]

        if len(buf) < FRAME_LEN:
            break

        frame = bytes(buf[:FRAME_LEN])
        if frame[-2:] != TAIL:
            del buf[0]          # false header / bad frame: resync by one byte
            continue

        codes = struct.unpack("<256h", frame[2:514])
        frames.append(codes)
        del buf[:FRAME_LEN]

    return frames
```

Notes:

- Keep at most the last byte when no header is found; this preserves a header split across two `read()` calls.
- `struct.unpack("<256h", ...)` expects exactly 512 payload bytes.
- The host may receive multiple frames in one read; process all complete frames, but only display the latest (or feed history for 2×/3×).

### 2.3 Reference packer (C)

```c
uint8_t frame[516];

frame[0] = 0xAA;
frame[1] = 0x55;

for (int i = 0; i < 256; i++) {
    int16_t v = (int16_t)adc[i];          /* 0..4095 */
    frame[2 + i * 2]     = (uint8_t)(v & 0xFF);
    frame[2 + i * 2 + 1] = (uint8_t)((v >> 8) & 0xFF);
}

frame[514] = 0x5A;
frame[515] = 0xA5;
```

Send all 516 bytes with USART TX DMA (or a blocking UART write). Do not insert delays between bytes.

---

## 3. UART settings

| Parameter | Value |
| --- | --- |
| Baud rate | 2,000,000 default (also tested at 921600 / 115200) |
| Data bits | 8 |
| Parity | None |
| Stop bits | 1 |
| Flow control | None |
| TX pin | PA9 (USART1_TX) |
| RX pin | PA10 (USART1_RX) — host → MCU is not used by the current firmware |

The firmware’s `USART_BRR_VALUE` is compiled for 72 MHz APB2:

- 2 Mbps → `0x24`;
- 921600 → `0x4E`;
- 115200 → `0x271`.

The host baud rate must match the firmware. A mismatch produces garbage bytes and no valid frames.

### 3.1 Baud vs. frame-rate ceiling

One frame = 516 bytes × 10 bits/byte = **5160 bits** (8-N-1). The UART alone limits the frame rate to:

| Baud | Theoretical max frames/s (5160 bits each) |
| ---: | ---: |
| 2,000,000 | 387.6 |
| 921,600 | 178.6 |
| 460,800 | 89.3 |
| 230,400 | 44.6 |
| 115,200 | 22.3 |

The actual firmware is slower because it also spends time sampling 256 points. At 2 Mbps the firmware’s frame rate is ~19–330 fps, so the UART is not the bottleneck at the low end.

---

## 4. Timing model (current polling firmware)

The stable firmware (`firmware/main.c`) runs this loop:

```
read PA1 pot ×4 → average
sample 256 × PA0 with delay
pack 516 bytes
USART1 TX DMA
wait for TX complete
repeat
```

### 4.1 Per-sample interval

With a 12 MHz ADC clock and 7.5-cycle sampling:

```
t_conv  = (7.5 + 12.5) / 12 MHz = 1.667 µs
t_delay = 0…200 µs        (PA1 potentiometer)
t_sample = t_conv + t_delay
```

So:

- sample rate = `1 / t_sample` ≈ **5 kSPS … 500 kSPS** (theoretical 600 kSPS at zero delay; polling overhead lowers it);
- 256-point window = `256 × t_sample` ≈ **0.427 ms … 51.2 ms**.

### 4.2 Frame period

```
t_tx     = 5160 / baud
t_frame  = 256 × t_sample + t_tx + software overhead
fps      = 1 / t_frame
```

At 2 Mbps, `t_tx = 2.58 ms`.

| PA1 delay | 256-point window | Frame period (incl. TX) | Approx. fps |
| ---: | ---: | ---: | ---: |
| 0 µs | 0.427 ms | 3.01 ms | ~333 |
| 20 µs | 5.55 ms | 8.12 ms | ~123 |
| 100 µs | 26.0 ms | 28.6 ms | ~35 |
| 200 µs | 51.2 ms | 53.8 ms | ~18.6 |

The firmware waits for the UART DMA to finish before starting the next frame. Therefore **frame period = sampling window + TX time**; no frames are intentionally dropped, and the host can derive the sample rate from the frame rate (legacy frequency estimator).

> **中文**：当前稳定固件是“采 256 点 → 打包 → DMA 发完 → 再采下一帧”。所以帧周期 = 采样窗口 + 串口发送时间；PA1 调的是每点延时 0–200 µs。上位机的时间轴/旧版频率估算都基于这个模型。

### 4.3 If the firmware changes

If a future firmware streams continuously (double buffer / circular DMA) instead of stopping during TX, the frame period no longer equals `window + t_tx`. The host’s legacy frequency estimator would become invalid; a timestamp or sample-interval field would be needed. Protocol v1 does not carry timing metadata.

---

## 5. Host-side processing (not part of the wire format)

The host applies the following after parsing:

- **Voltage**: `code × 3.3 / 4095`.
- **Auto Y**: min/max of visible frames, 10% margin, 0.2 V minimum span.
- **Persistence**: keep 1–20 frames, fade old frames over ~50 ms.
- **Trigger**:
  - level 0–3.3 V, rising/falling edge;
  - 2% hysteresis to avoid noise retriggering;
  - fractional crossing interpolation;
  - trigger point fixed at the **left edge**;
  - missing tail samples become `NaN` (no circular wrap).
- **1×/2×/3×**: 1× uses the latest frame; 2×/3× concatenate 2/3 consecutive frames into 512/768 points.
  - This is **frame concatenation**, not continuous hardware sampling;
  - seams can show phase jumps because of the UART gap between frames.

### 5.1 Legacy frequency estimate

The 1.4–1.5 host versions estimated frequency as:

```
fps       = frames received / elapsed time
t_window  = 1 / fps - t_tx
fs        = 256 / t_window
f         = (zero_crossings - 1) / (first-to-last crossing time)
```

The result was smoothed with a 7-sample median and displayed with `≈`.

This was removed from the stable 1.6+ host because:

- it depends on the firmware’s “stop during TX” timing;
- frame drops or 2×/3× concatenation make it unreliable;
- it is an estimate, not a hardware frequency counter.

---

## 6. Error handling and limitations

- **No CRC**: a corrupted payload with a valid header/tail is accepted. Use a good USB-UART cable and keep the link short.
- **No sequence number**: the host cannot tell whether a frame was dropped; it only sees the next valid frame.
- **No timestamp**: timing is inferred from arrival rate and known firmware behavior.
- **False headers**: payload bytes can contain `AA 55`; the parser validates the tail and resyncs by one byte.
- **Truncated frames**: a UART glitch can leave a partial frame; the parser keeps the last byte and scans again.
- **2 Mbps**: many CH340/CP2102/FT232 adapters work, but cheap cables or hubs may not. If frames are unstable, lower the firmware and host baud together.
- **Input range**: 0–3.3 V only. Do not connect negative voltages or >3.3 V without external protection/attenuation.
- **Single channel**: one PA0 signal per frame; PA1 is used for the sample-rate potentiometer.

---

## 7. Compatibility

- The 516-byte frame layout is **protocol v1** and is frozen.
- All stable host versions since 1.0 can parse frames from the stable firmware.
- Host **1.9.1** adds editable COM-port/baud input (300–4,000,000); this is a host UI change only. The frame format, baud requirements and parser are unchanged.
- The experimental timer-triggered ADC + DMA and MCP4725 DAC branches used the same 516-byte frame layout; they are not part of the current stable firmware.
- Changing `BUF_SIZE`, frame header/tail, endianness or baud requires a new protocol version and matching host changes.
