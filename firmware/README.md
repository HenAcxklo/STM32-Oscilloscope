# Firmware — STM32F103C8T6 acquisition

`main.c` is the stable acquisition firmware for the serial oscilloscope. It streams 516-byte frames to the PC over USART1 at 2 Mbaud.

## 1. What it does

- HSE 8 MHz → PLL ×9 → **72 MHz**; ADC clock 12 MHz.
- **PA0**: analog input, 7.5-cycle sampling.
- **PA1**: 10 kΩ potentiometer wiper, 55.5-cycle sampling; maps to a 0–200 µs delay between PA0 samples.
- Per frame: 4× PA1 average → delay → 256× PA0 → 516-byte frame → USART1 TX DMA → wait → repeat.
- Frame: `AA 55 + 256 × int16 little-endian + 5A A5` = 516 bytes.
- USART1: 8-N-1, 2,000,000 bps, `BRR = 0x24`.
- No DAC and no timer-triggered ADC in this stable build; those were experimental branches.

## 2. Keil project setup

The repository ships only `main.c`; create a Keil MDK-ARM project (or add this file to your existing STM32F103 project).

| Setting | Value |
| --- | --- |
| Device | `STM32F103C8` |
| Core | Cortex-M3 |
| Flash / RAM | 64 KB / 20 KB |
| Compiler | ARMCC 5.06 (Keil MDK 5.x) |
| C standard | C99 |
| Defines | `STM32F10X_MD`, `USE_STDPERIPH_DRIVER` |
| Startup | `startup_stm32f10x_md.s` |
| System | `system_stm32f10x.c` |
| Library | STM32F1 StdPeriph: `stm32f10x_rcc.c`, `gpio.c`, `adc.c`, `usart.c`, `dma.c`, `flash.c`, `misc.c` |
| Include paths | `Start/`, `Library/`, `User/` (or your StdPeriph layout) |
| Output | ST-Link, flash at `0x08000000` |

`main.c` uses direct register access, so you only need the StdPeriph headers/sources to provide the CMSIS device definitions and startup code.

## 3. Clock and baud

- 8 MHz HSE → PLL ×9 = 72 MHz.
- APB2 = 72 MHz (USART1, ADC prescaler /6 → 12 MHz).
- APB1 = 36 MHz.
- `USART_BRR_VALUE` in `main.c`:

| Baud | BRR |
| ---: | ---: |
| 2,000,000 | `0x24` |
| 921,600 | `0x4E` |
| 115,200 | `0x271` |

Change both the firmware BRR and the host baud rate together.

## 4. Flashing

1. Connect ST-Link: `SWDIO → PA13`, `SWCLK → PA14`, `GND → GND`, and connect ST-Link 3.3 V/VREF to the board’s 3.3 V if your probe requires it.
2. Power the board (USB or 3.3 V).
3. In Keil: **Options → Debug → ST-Link**, **Flash Download → STM32F10x Med-density Flash**.
4. Build and Download (or use STM32CubeProgrammer).

Some ST-Link V2-1 probes refuse to connect when the target VREF pin is low; connect VREF/3.3 V or use a V2 clone that does not check it.

## 5. Wiring

| STM32 | Connect |
| --- | --- |
| PA0 | signal, 0–3.3 V only |
| PA1 | 10 kΩ pot wiper; ends to 3.3 V and GND |
| PA9 | USB-UART RX |
| PA10 | USB-UART TX |
| GND | USB-UART GND (common ground) |
| 3.3V | 3.3 V rail |

Use a USB-UART adapter rated for 2 Mbaud. CH340, CP2102 and FT232 normally work; some cheap cables/hubs do not.

## 6. Troubleshooting

**No frame / host sees 0 fps**

- TX/RX not crossed (PA9 → adapter RX, PA10 ← adapter TX);
- GND not common;
- baud mismatch (host must be 2,000,000 by default);
- firmware stuck waiting for HSE: some Blue Pill clones have a bad 8 MHz crystal. The code waits forever on `HSERDY`. Check the crystal or use an HSI-based clock build.

**Voltage is wrong / stuck**

- PA0 must be 0–3.3 V; do not exceed 3.3 V;
- host `VOLTS_PER_LSB` must be `3.3 / 4095`;
- if the trace shows the potentiometer voltage, check that PA0 and PA1 are not swapped, and that PA0 is actually connected to the signal.

**2 Mbps is unreliable**

- Lower both firmware and host to 921,600 or 115,200;
- shorten the USB-UART wires;
- try a different adapter/cable;
- avoid USB hubs.

**Board does not boot after flashing**

- Check BOOT0 = 0 (flash boot), BOOT1 = 0;
- check the 3.3 V rail;
- use ST-Link “Connect under reset” if the MCU is stuck.

## 7. Experimental branches (not included)

The following were developed and tested but are not part of this stable firmware:

- TIM3-triggered ADC + DMA (hardware-timed sampling, independent of DAC/I2C interrupts);
- MCP4725 DAC sine output on I2C1 (PB6/PB7, 400 kHz, TIM4 3.2 kHz / 64 points ≈ 50 Hz).

They keep the same 516-byte frame format. If you need them, develop them on a separate branch and re-validate the host timing/frequency model.
