#!/usr/bin/env python3
import socket
import sys

MAGIC = b"Qspt1WmJOL"
PORT = 50000

print(f"Listening on UDP port {PORT}...")
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
try:
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
except:
    pass
sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
sock.bind(("", PORT))

count = 0
try:
    while True:
        data, addr = sock.recvfrom(1024)
        count += 1
        has_magic = data[:len(MAGIC)] == MAGIC
        pkt_type = data[10] if len(data) > 10 else 0xFF
        device_name = data[12:32].rstrip(b"\x00").decode("utf-8", errors="replace") if len(data) >= 32 else "???"
        device_number = data[36] if len(data) > 36 else 0xFF
        ip = ".".join(str(b) for b in data[44:48]) if len(data) >= 48 else "???"
        status = "✓" if has_magic else "✗"
        print(f"[{count}] {status} Type:0x{pkt_type:02X} Dev#{device_number} '{device_name}' from {addr[0]} IP:{ip} Len:{len(data)}")
except KeyboardInterrupt:
    print("\nStopped.")
finally:
    sock.close()
