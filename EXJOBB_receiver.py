"""
Usage:
    python EXJOBB_receiver.py [options]

    --port      UDP port to listen on     (default: 5006)
    --bind      IP address to bind to     (default: 0.0.0.0)
    --duration  Stop after N seconds      (default: 0 = run until Ctrl+C)
    --label     Label for this run        (default: stream)

Examples (run on RECEIVER machine 192.168.20.10):

    python EXJOBB_receiver.py --port 5006 --label 1080p --duration 60
    python EXJOBB_receiver.py --port 5004 --label voice --duration 60

Start receiver BEFORE starting sender.py.
No NTP sync or clock alignment required.
"""

import argparse, socket, struct, sys, time, platform, ctypes
from collections import deque

#RTP parser

RTP_HEADER_MIN = 12

def parse_rtp_basic(data: bytes) -> dict | None:
    if len(data) < RTP_HEADER_MIN:
        return None
    b0, b1 = data[0], data[1]
    if (b0 >> 6) != 2:
        return None
    seq, ts, ssrc = struct.unpack_from('!HII', data, 2)
    pt = b1 & 0x7F
    return dict(seq=seq, ts=ts, ssrc=ssrc, pt=pt)

def seq_diff(a: int, b: int) -> int:
    d = (a - b) & 0xFFFF
    return d if d < 0x8000 else d - 0x10000

CLOCK_RATES = {
    0: 8000, 8: 8000, 9: 8000, 18: 8000,
    96: 90000, 97: 90000, 98: 90000, 99: 90000,
    }
def clock_rate_for(pt:int) -> int:
    return CLOCK_RATES.get(pt, 90000)

class StreamStats:
    def __init__(self, ssrc: int, clock_rate: int):
        self.ssrc = ssrc
        self.clock_rate = clock_rate
        self.received = 0
        self.lost = 0 
        self._next_seq = None
        self._last_transit = None
        self.jitter = 0.0
        self._bkt_bytes = 0
        self._bkt_start = None
        self.kbps_log = []
    
    def update(self, seq: int, ts: int, arrival: float, raw_len: int):
        if self._next_seq is None:
            self._next_seq = (seq + 1) & 0xFFFF
            self._bkt_start = arrival
        else:
            diff = seq_diff(seq, (self._next_seq - 1 ) & 0xFFFF)
            if diff > 0:
                self.lost += diff - 1
                self._next_seq = (seq + 1) & 0xFFFF
            elif diff == 0:
                self._next_seq = (seq + 1) & 0xFFFF

        self.received += 1

        transit = arrival * self.clock_rate - ts
        if self._last_transit is not None:
            d = abs(transit - self._last_transit)
            self.jitter += (d - self.jitter) / 16.0
        self._last_transit = transit

        self._bkt_bytes += raw_len
        if arrival - self._bkt_start >= 1.0:
            elapsed = arrival - self._bkt_start
            self.kbps_log.append(self._bkt_bytes * 8 / elapsed / 1000.0)
            self._bkt_bytes = 0
            self._bkt_start = arrival
    
    def jitter_ms(self) -> float:
        return (self.jitter /self.clock_rate ) * 1000.0
    
    def loss_pct(self) -> float:
        total = self.received + self.lost
        return (self.lost / total * 100) if total > 0 else 0.0
    
    def current_kbps(self) -> float:
        return self.kbps_log[-1] if self.kbps_log else 0.0
    
def main():
    parser = argparse.ArgumentParser(
        description='RTP echo receiver — reflects packets to sender for RTT measurement'
    )
    parser.add_argument('--port', type=int, required=True,help='UDP listen port (5006=video, 5004=voice)')
    parser.add_argument('--bind',     default='0.0.0.0',help='IP to bind to')
    parser.add_argument('--duration', type=float, default=0, help='Auto-stop after N seconds (0=until Ctrl+C)')
    parser.add_argument('--label',    default='stream', help='Test label for display')
    parser.add_argument('--echo-ip', default=None, help='IP to echo packets back to (overrides learned sender IP)')

    args = parser.parse_args()

    echo_port = args.port + 1

    recv_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    recv_sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 * 1024 * 1024)
    try:
        recv_sock.bind((args.bind, args.port))
    except OSError as e:
        print(f"\n[Receiver] ERROR: Cannot bind to port {args.port}: {e}")
        print("  → Check no other process uses this port")
        print("  → Add a Windows Firewall inbound rule for UDP on this port")
        sys.exit(1)
    recv_sock.settimeout(2.0)

    echo_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    echo_sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 8 * 1024 * 1024)

    print(f"\n[Receiver] Listening on  {args.bind}:{args.port}")
    print(f"[Receiver] Echoing to    sender:{echo_port}")
    print(f"[Receiver] Label         : {args.label}")
    if args.duration > 0:
        print(f"[Receiver] Auto-stops after {args.duration:.0f}s")
    print("[Receiver] Waiting for stream...  (Ctrl+C to stop)\n")
    print(f"  {'Elapsed':>7}  {'Pkts':>8}  {'Lost':>6}  {'Jitter':>9}  {'Kbps':>9}")
    print(f"  {'-'*7}  {'-'*8}  {'-'*6}  {'-'*9}  {'-'*9}")

    streams = {}
    start_time = time.perf_counter()
    last_print = start_time
    sender_ip = None

    try:
        while True:
            now_perf = time.perf_counter()
            if args.duration > 0 and (now_perf - start_time) >= args.duration:
                print(f"\n[Receiver] {args.duration:.0f}s duration reached.")
                break
            try:
                data, addr = recv_sock.recvfrom(65535)
            except socket.timeout:
                continue

            arrival = time.perf_counter()
            pkt = parse_rtp_basic(data)
            if pkt is None:
                continue
            
            if sender_ip is None:
                sender_ip = args.echo_ip if args.echo_ip else addr[0]
                print(f"  [+] Stream from {addr[0]}  — echoing to {sender_ip}:{echo_port}\n")
            echo_sock.sendto(data, (sender_ip, echo_port))

            ssrc = pkt['ssrc']
            if ssrc not in streams:
                cr = clock_rate_for(pkt['pt'])
                streams[ssrc] = StreamStats(ssrc, cr)

            streams[ssrc].update(pkt['seq'], pkt['ts'], arrival, len(data))

            if arrival - last_print >= 1.0:
                elapsed = arrival - start_time
                rx = sum(s.received for s in streams.values())
                lost = sum(s.lost for s in streams.values())
                jitter = sum(s.jitter_ms() for s in streams.values()) / len(streams)
                kbps = sum(s.current_kbps() for s in streams.values())
                print(f"  {elapsed:>6.1f}s  {rx:>8}  {lost:>6}  "
                      f"{jitter:>8.3f}ms  {kbps:>8.1f}")
                last_print = arrival    
    except KeyboardInterrupt:
        print("\n[Receiver] Stopped by user.")
    finally:
        recv_sock.close()
        echo_sock.close()

        elapsed = time.perf_counter() - start_time
        rx = sum (s.received for s in streams.values())
        lost = sum(s.lost for s in streams.values())

        print()
        print("=" * 50)
        print(f"  RECEIVER SUMMARY  —  {args.label}  —  {elapsed:.1f}s")
        print("=" * 50)
        if streams:
            print(f"  Packets received : {rx}")
            print(f"  Packets lost     : {lost}  ({lost/(rx+lost)*100:.2f}%)" if rx+lost else "")
            for ssrc, st in streams.items():
                print(f"  SSRC 0x{ssrc:08X}  jitter={st.jitter_ms():.3f} ms")
        else:
            print("  No RTP packets received.")
            print(f"  → Is sender.py targeting this machine on port {args.port}?")
        print()
        print("[Receiver] Done.")


if __name__ == '__main__':
    main()

