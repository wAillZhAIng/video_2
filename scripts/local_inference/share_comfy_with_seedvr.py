"""Give the legacy SeedVR2 adapter a loopback endpoint on the existing ComfyUI."""
from contextlib import suppress
from select import select
import socket
from socketserver import BaseRequestHandler, ThreadingTCPServer


class SeedVRClient(BaseRequestHandler):
    def handle(self):
        try:
            with socket.create_connection(('127.0.0.1', 8188), timeout=5) as comfy:
                self.request.settimeout(120)
                comfy.settimeout(120)
                peers = {self.request: comfy, comfy: self.request}
                while peers:
                    readable, _, _ = select(list(peers), [], [], 120)
                    if not readable:
                        break
                    for source in readable:
                        destination = peers[source]
                        chunk = source.recv(65536)
                        if chunk:
                            destination.sendall(chunk)
                        else:
                            del peers[source]
                            with suppress(OSError):
                                destination.shutdown(socket.SHUT_WR)
        except OSError:
            pass


class ComfyLoopbackRelay(ThreadingTCPServer):
    daemon_threads = True


if __name__ == '__main__':
    with ComfyLoopbackRelay(('127.0.0.1', 8189), SeedVRClient) as relay:
        print('SeedVR2 compatibility endpoint: 127.0.0.1:8189 -> 127.0.0.1:8188', flush=True)
        relay.serve_forever()
