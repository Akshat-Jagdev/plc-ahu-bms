#!/usr/bin/env python3
"""Check the Modbus mapping of this OpenPLC build against the running io_probe program.

Deploy the probe first, then run with the project venv:

    python3 tools/deploy.py tools/io_probe.st
    .venv/bin/python tools/io_probe.py

Exits non-zero if any expectation fails. Run it again whenever the OpenPLC image changes;
docs/address-map.md records the last verified result.
"""
import sys
import time

from pymodbus.client import ModbusTcpClient
from pymodbus.exceptions import ConnectionException, ModbusException

HOST = "127.0.0.1"
PORT = 502
UNIT = 1

H_MW0, H_MW1, H_MW100 = 1024, 1025, 1124   # holding register -> %MW0, %MW1, %MW100
WRITTEN = {H_MW0: 258, H_MW1: 300, H_MW100: 0x1234}  # 258 = 0x0102 exposes byte swaps
EXPECTED_COILS_ON = [0, 7, 8]              # %QX0.0, %QX0.7, %QX1.0
EXPECTED_HOLDING = [258, 259, 0x1234, 300]

results = []


def check(name, ok, detail=""):
    results.append(ok)
    print("%s  %s%s" % ("PASS" if ok else "FAIL", name, "  [%s]" % detail if detail else ""))


def read(client, method, address, count):
    resp = getattr(client, method)(address, count=count, device_id=UNIT)
    if resp.isError():
        raise ModbusException("%s(%d, %d) returned %s" % (method, address, count, resp))
    return resp


def run(client):
    written_at = time.monotonic()
    for address, value in WRITTEN.items():
        if client.write_register(address, value, device_id=UNIT).isError():
            raise ModbusException("write to holding register %d failed" % address)

    latency = None
    for _ in range(200):
        regs = read(client, "read_holding_registers", 0, 1).registers
        if regs[0] == WRITTEN[H_MW0]:
            latency = (time.monotonic() - written_at) * 1000.0
            break
        time.sleep(0.002)
    time.sleep(0.3)
    holding = read(client, "read_holding_registers", 0, 5).registers

    coils = read(client, "read_coils", 0, 16).bits[:16]
    discrete = read(client, "read_discrete_inputs", 0, 16).bits[:16]
    input_regs = read(client, "read_input_registers", 0, 8).registers

    # outputs are program-owned: a master write to a coil must not survive a scan
    client.write_coil(1, True, device_id=UNIT)
    time.sleep(0.3)
    coil1_after_write = read(client, "read_coils", 1, 1).bits[0]

    # nominal 10 s PLC window timed against the wall clock
    first = None
    window_s = None
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline:
        count = read(client, "read_holding_registers", 4, 1).registers[0]
        now = time.monotonic()
        if first is None:
            if count >= 1:
                first = (count, now)
        elif count >= first[0] + 1:
            window_s = now - first[1]
            break
        time.sleep(0.05)

    print("coils ON:            %s" % [i for i, bit in enumerate(coils) if bit])
    print("discrete inputs ON:  %s" % [i for i, bit in enumerate(discrete) if bit])
    print("input registers:     %s" % input_regs)
    print("holding registers 0-4: %s" % holding)
    print("write visible after: %s" % ("%.0f ms" % latency if latency is not None else "never"))
    if window_s is not None:
        print("nominal 10 s window: %.2f s on the wall clock" % window_s)

    check("%%QX bits map to consecutive coils %s" % EXPECTED_COILS_ON,
          [i for i, bit in enumerate(coils) if bit] == EXPECTED_COILS_ON)
    check("%%MW0 -> holding %d, big-endian" % H_MW0, holding[0] == EXPECTED_HOLDING[0], holding[0])
    check("%MW0 + 1 -> %QW1", holding[1] == EXPECTED_HOLDING[1], holding[1])
    check("%%MW100 -> holding %d" % H_MW100, holding[2] == EXPECTED_HOLDING[2], holding[2])
    check("%%MW1 -> holding %d" % H_MW1, holding[3] == EXPECTED_HOLDING[3], holding[3])
    check("coils are program-owned (write to coil 1 did not stick)", coil1_after_write is False)
    check("task interval honoured (10 s window within 5 %)",
          window_s is not None and 9.5 <= window_s <= 10.5,
          "%.2f s" % window_s if window_s is not None else "timeout")

    print("%d/%d checks passed" % (sum(results), len(results)))
    return all(results)


def main():
    client = ModbusTcpClient(HOST, port=PORT)
    try:
        if not client.connect():
            raise ConnectionException("connect() returned False")
        ok = run(client)
    except ConnectionException as exc:
        sys.exit("Modbus TCP is not answering on %s:%d (%s). Is the runtime running?"
                 % (HOST, PORT, exc))
    finally:
        client.close()
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
