# Address map (as built, verified 2026-11-25)

Runtime: OpenPLC in the `wzy318/openplc` container, Modbus TCP slave on port 502, unit 1.
Verified against the running runtime with `tools/io_probe.py` (7 of 7 checks) and by reading
`webserver/core/modbus.cpp` in the image.

## How this runtime maps IEC locations to Modbus

| IEC location | Modbus table | Function codes | Address |
|---|---|---|---|
| `%QX0.0` ... | Coils | 1 read, 5 / 15 write | bit-packed: `%QX<byte>.<bit>` is coil `byte * 8 + bit` |
| `%QW0` ... | Holding registers | 3 read, 6 / 16 write | register n (0-1023) |
| `%MW0` ... | Holding registers | 3 read, 6 / 16 write | register 1024 + n |
| `%IX0.0` ... | Discrete inputs | 2 read only | bit-packed, same rule as coils |
| `%IW0` ... | Input registers | 4 read only | register n |

16-bit values are standard big-endian on the wire. Outputs are program-owned: a Modbus
write to a coil is overwritten on the next scan. The scan period is the TASK interval in
the .st file (50 ms here; a nominal 10 s timer measured 10.07 s on the wall clock).

**Inputs cannot be written over Modbus on this runtime.** A real controller gets its %I
points from the I/O hardware. Here the field side writes an input image into %MW and the
program reads it through one mapping section; on real hardware that section would read
%IX and %IW instead.

## Input image (written by the field side, one register per point)

| Point | Tag | Location | Holding | Meaning |
|---|---|---|---|---|
| DI1 | SF_RUN_CMD_FB | %MW100 | 1124 | 1 = supply fan running (contactor feedback) |
| DI2 | SF_TRIP | %MW101 | 1125 | 1 = supply fan tripped |
| DI3 | RF_TRIP | %MW102 | 1126 | 1 = return fan tripped |
| DI4 | SMK_INTLK | %MW103 | 1127 | N/C smoke loop: 1 = closed = healthy |
| DI5 | FS_INTLK | %MW104 | 1128 | N/C gas-leak loop: 1 = closed = healthy |
| DI6 | FILT_DP_HS | %MW105 | 1129 | 1 = filter differential-pressure switch |
| DI7 | LO_OA_T | %MW106 | 1130 | 1 = outdoor air too low, opening the dampers is locked out |
| DI8 | MAN_AUTO | %MW107 | 1131 | 1 = Auto, 0 = Hand |
| DI9 | OCC_SCHED | %MW108 | 1132 | 1 = occupied schedule (as built) |
| DI10 | TRIP_RESET | %MW109 | 1133 | 1 = reset push-button held (as built) |
| AI1 | SAT | %MW110 | 1134 | supply-air temperature, 0-40 C |
| AI2 | RAT | %MW111 | 1135 | return-air temperature, 0-40 C |
| AI3 | OAT | %MW112 | 1136 | outdoor-air temperature, 0-50 C |
| AI4 | CO2 | %MW113 | 1137 | space CO2, 0-2000 ppm |
| AI5 | DP_SAT | %MW114 | 1138 | filter differential pressure, 0-500 Pa |

Analog values are 0-10 V signals carried as 0-10000 counts:
engineering value = counts / 10000 * span.

## Outputs (program-owned)

| Point | Tag | Location | Modbus | Meaning |
|---|---|---|---|---|
| DO1 | SF_CMD | %QX0.0 | coil 0 | supply fan start |
| DO2 | RF_CMD | %QX0.1 | coil 1 | return fan start |
| DO3 | EXD_CMD | %QX0.2 | coil 2 | exhaust damper open |
| DO4 | FAD_CMD | %QX0.3 | coil 3 | fresh-air damper open |
| DO6 | ALM_GEN | %QX0.4 | coil 4 | general alarm |
| DO7 | CHW_PUMP_CMD | %QX0.5 | coil 5 | chilled-water pump enable |
| AO1 | CHWV | %QW0 | holding 0 | valve position, 0-10000 = 0-100 % |
| - | STATE | %QW1 | holding 1 | sequence state code (as built) |
| - | ALARM | %QW2 | holding 2 | alarm word (as built) |

## State code (%QW1)

| 0 | 1 | 2 | 3 | 4 | 5 | 6 | 7 |
|---|---|---|---|---|---|---|---|
| IDLE | DAMPERS | RF | SF | RUN | PURGE | TRIPPED | HAND |

## Alarm word (%QW2)

| Bit | Value | Meaning | Behaviour |
|---|---|---|---|
| 0 | 1 | Supply-fan trip | latched until reset |
| 1 | 2 | Return-fan trip | latched until reset |
| 2 | 4 | Smoke interlock open | latched until reset |
| 3 | 8 | Gas-leak interlock open | latched until reset |
| 4 | 16 | Supply-fan feedback lost or not proven in time | latched until reset |
| 5 | 32 | Filter differential pressure high | clears with the input |
| 6 | 64 | CO2 above setpoint while running | clears with the condition |
| 7 | 128 | Low outdoor-air lockout active | status |

Only the first cause of a trip is latched, so the word shows the first-out.
ALM_GEN is any of bits 0-5.

## Timing

- Start: dampers 10 s, return fan 5 s, supply fan 5 s with run feedback, then RUN.
- Normal stop: valve to 0, supply fan off, 10 s purge on the return fan, dampers to minimum.
- Trip: every output off in the scan the trip is detected. On a smoke trip the exhaust
  damper opens after a 10 s fan spin-down.
