#!/usr/bin/env python3
"""Notes daemon — tiny TCP service used as the framework smoke test.

Protocol (newline-terminated, one command per connection):
  PUT <text>   -> 'OK <id>'
  GET <id>     -> the stored text, or 'ERR'
  LIST         -> 'COUNT <n>'
"""

import socket
import sys
import threading

STORE: list[str] = []
LOCK = threading.Lock()


def handle(conn: socket.socket) -> None:
    try:
        conn.settimeout(5)
        data = b''
        while not data.endswith(b'\n'):
            chunk = conn.recv(4096)
            if not chunk:
                return
            data += chunk
        line = data.decode(errors='replace').strip()
        if line.startswith('PUT '):
            payload = line[4:]
            with LOCK:
                STORE.append(payload)
                idx = len(STORE) - 1
            conn.sendall(f'OK {idx}\n'.encode())
        elif line.startswith('GET '):
            try:
                idx = int(line[4:])
                with LOCK:
                    out = STORE[idx]
                conn.sendall(out.encode() + b'\n')
            except Exception:
                conn.sendall(b'ERR\n')
        elif line == 'LIST':
            with LOCK:
                n = len(STORE)
            conn.sendall(f'COUNT {n}\n'.encode())
        else:
            conn.sendall(b'ERR\n')
    finally:
        conn.close()


def main(port: int) -> None:
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(('0.0.0.0', port))
    s.listen(64)
    while True:
        c, _ = s.accept()
        threading.Thread(target=handle, args=(c,), daemon=True).start()


if __name__ == '__main__':
    main(int(sys.argv[1]))
