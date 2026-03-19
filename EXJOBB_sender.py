"""
Usage:
    python EXJOBB_sender.py --test <test> [--dest <ip>] [--dry]

    --test   1080p   1920x1080 H.264 at 5 Mbps CBR   → port 5006
             4k      3840x2160 H.264 at 20 Mbps CBR  → port 5006
             voice   G.711 a-law 8 kHz               → port 5004
    --dest   Receiver IP  (default: 192.168.20.10)
    --dry    Print FFmpeg command and exit without sending

Examples:
    python EXJOBB_sender.py --test 1080p
    python EXJOBB_sender.py --test voice --dest 192.168.20.10
"""
import argparse, json, socket, statistics, struct, subprocess, sys, threading, time, multiprocessing
# high-resolution wall clock
_TIME_ANCHOR_WALL = time.time()
_TIME_ANCHOR_PERF = time.perf_counter()

def hires_time() -> float:
    return _TIME_ANCHOR_WALL + (time.perf_counter() - _TIME_ANCHOR_PERF)

# RTP / extension constants

RTP_HEADER_MIN = 12
EXT_PROFILE_ID = 0x5453
EXT_WORDS = 3

# Timestamp injection
def inject_timestamp(packet: bytes, send_seq:int, send_time: float) -> bytes:
    if len(packet) < RTP_HEADER_MIN:
        return packet
    
    b0 = packet[0]
    cc = b0 & 0x0F
    has_ext_= (b0 >> 4) & 0x01
    hlen = RTP_HEADER_MIN + cc * 4

    our_data = struct.pack('!Id', send_seq, send_time)
    our_ext = struct.pack('!HH', EXT_PROFILE_ID,EXT_WORDS) + our_data

    if has_ext_:
        if len(packet) < hlen + 4:
            return packet
        existing_len = struct.unpack_from('!H', packet, hlen + 2)[0]
        existing_data = packet[hlen + 4 : hlen + 4 + existing_len * 4:]
        new_ext = struct.pack('!HH', EXT_PROFILE_ID, EXT_WORDS + existing_len) + \
                  our_data + existing_data
        return packet[:hlen] + new_ext + packet[hlen + 4 + existing_len * 4:]
    else:
        new_b0 = b0 | 0x10
        return bytes([new_b0]) + packet[1:hlen] + our_ext + packet[hlen:]
    
def extract_send_info(packet: bytes) -> tuple:
    # Return send_seq, send_time, rtp_seq, rtp_ts
    if len(packet) < RTP_HEADER_MIN:
        return None,None,None,None, None
    
    b0 = packet[0]
    if not ((b0 >> 4)& 0x01):
        return None,None,None,None, None
    
    cc = b0 & 0x0F
    hlen = RTP_HEADER_MIN + cc * 4

    if len(packet) < hlen + 4:
        return None,None,None,None, None
    
    profile_id = struct.unpack_from('!H', packet, hlen)[0]
    ext_words = struct.unpack_from('!H', packet, hlen + 2)[0]

    if profile_id != EXT_PROFILE_ID or ext_words < EXT_WORDS:
        return None,None,None,None, None
    
    if len(packet) < hlen + 4 + 12:
        return None,None,None,None, None
    
    send_seq, send_time = struct.unpack_from('!Id', packet, hlen + 4)
    rtp_seq = struct.unpack_from('!H', packet, 2)[0]
    rtp_ts = struct.unpack_from('!I', packet, 4)[0]
    pt = packet[1] & 0x7f
    return send_seq, send_time, rtp_seq,rtp_ts, pt

# FFmpeg pipeline builder
LOOPBACK_PORT = 59876

CLOCK_RATES = {0: 8000, 8: 8000, 9:8000, 18:8000, 96: 90000, 97: 90000, 98: 90000, 99:90000}

def clock_rate_for(pt: int) -> int:
    return CLOCK_RATES.get(pt, 90000)

def build_ffmpeg_cmd(test:str) -> tuple:
    #Return ffmpeg_argv, dest_port_on_receiver
    base = ['ffmpeg', '-hide_banner']

    if test == '1080p':
        return base + [
            '-re', '-f', 'lavfi',
            '-i', 'testsrc=size=1920x1080:rate=30',
            '-c:v', 'libx264',
            '-preset', 'ultrafast', '-tune', 'zerolatency',
            '-b:v', '5M', '-minrate', '5M', '-maxrate', '5M', '-bufsize', '5M',
            '-x264-params', 'nal-hrd=cbr',
            '-pix_fmt', 'yuv420p', '-t', '60',
            '-f', 'rtp', f'rtp://127.0.0.1:{LOOPBACK_PORT}',
        ], 5006

    elif test == '4k':
        return base + [
            '-re', '-f', 'lavfi',
            '-i', 'testsrc=size=3840x2160:rate=30',
            '-c:v', 'libx264',
            '-preset', 'ultrafast', '-tune', 'zerolatency',
            '-b:v', '20M', '-minrate', '20M', '-maxrate', '20M', '-bufsize', '20M',
            '-x264-params', 'nal-hrd=cbr',
            '-pix_fmt', 'yuv420p', '-t', '60',
            '-f', 'rtp', f'rtp://127.0.0.1:{LOOPBACK_PORT}',
        ], 5006

    elif test == 'voice':
        return base + [
            '-re', '-f', 'lavfi',
            '-i', 'sine=frequency=1000:sample_rate=8000',
            '-c:a', 'pcm_alaw', '-ar', '8000', '-ac', '1',
            '-payload_type', '8', '-t', '60',
            '-f', 'rtp', f'rtp://127.0.0.1:{LOOPBACK_PORT}',
        ], 5004

    else:
        raise ValueError(f"Unknown test: '{test}'. Choose: 1080p, 4k, voice")

def check_ffmpeg():
    try:
        r = subprocess.run(
            ['ffmpeg', '-version'],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        if r.returncode != 0:
            raise FileNotFoundError
    except FileNotFoundError:
        print("[Sender] ERROR: FFmpeg not found on PATH.")
        print("  Download: https://www.gyan.dev/ffmpeg/builds/")
        print("  Add bin\\ to System PATH and restart your terminal.")
        sys.exit(1)
# Send-side throughput tracker
class SendMetrics:
    def __init__(self):
        self._lock = threading.Lock()
        self.bytes_log = []

    def record(self, t: float, raw_len: int):
        with self._lock:
            self.bytes_log.append((t, raw_len))

    def throughput_kbps(self) -> dict:
        with self._lock:
            log = list(self.bytes_log)
        if len(log) < 2:
            return {}
        buckets = []
        bkt_start = log[0][0]
        bkt_bytes = 0
        for t, b in log:
            bkt_bytes += b
            if t - bkt_start >= 1.0:
                buckets.append(bkt_bytes * 8 / (t - bkt_start) / 1000.0)
                bkt_bytes = 0
                bkt_start = t
        if not buckets:
            return {}
        std = statistics.stdev(buckets) if len(buckets) > 1 else 0.0
        return{
            "mean_kbps":  round(statistics.mean(buckets), 2),
            "min_kbps":   round(min(buckets), 2),
            "max_kbps":   round(max(buckets), 2),
            "stdev_kbps": round(std, 2),
            "buckets":    buckets,
        }
    def current_kbps(self) -> float:
        with self._lock:
            log = list(self.bytes_log)
        if len(log) < 2:
            return 0.0
        bkt_start = log[-1][0] - 1.0
        bkt_bytes = sum(b for t, b in log if t >= bkt_start)
        elapsed =log[-1][0] - max(log[0][0], bkt_start)
        if elapsed <= 0:
            return 0.0
        return bkt_bytes * 8 / elapsed / 1000.0

#Shared RTT store
class RttStore:
    def __init__(self):
        self._lock = threading.Lock()
        self.rtts =[]
        self.recent =[]
    
    def add(self, rtt_ms: float):
        with self._lock:
            self.rtts.append(rtt_ms)
            self.recent.append(rtt_ms)
            if len(self.recent) > 200:
                self.recent.pop(0)
    
    def snapshot(self):
        with self._lock:
            return list(self.rtts)
class StreamMetrics:
    def __init__(self):
        self._lock = threading.Lock()
        self.seq_log= []
        self.arrival_log = []
        self.bytes_log = []
        self.rtp_ts_log = []
        self.clock_rate = 90000
        self._last_transit = None
        self.jitter = 0.0
    
    def update(self, seq: int, rtp_ts: int, arrival: float, raw_len: int, clock_rate : int):
        with self._lock:
            self.clock_rate = clock_rate
            self.seq_log.append(seq)
            self.arrival_log.append(arrival)
            self.bytes_log.append((arrival, raw_len))

            transit = arrival * clock_rate - rtp_ts
            if self._last_transit is not None:
                d = abs(transit - self._last_transit)
                self.jitter += (d - self.jitter) / 16.0
            self._last_transit = transit
    
    def jitter_ms(self) -> float:
        return (self.jitter / self.clock_rate) * 1000
    def loss_and_bursts(self):
        with self._lock:
            seqs = self.seq_log
        if len(seqs) < 2:
            return 0, 0.0, 0, 0
        
        lost = 0
        max_burst = 0
        burst_count = 0
        in_burst = False 
        cur_burst = 0
        
        for i in range(1, len(seqs)):
            gap = (seqs[i] - seqs[i-1]) & 0xFFFF
            if gap == 1:
                if in_burst:
                    burst_count += 1
                    in_burst = False
                    cur_burst = 0
            elif gap > 1:
                dropped = gap - 1
                lost += dropped
                cur_burst += dropped
                max_burst = max(max_burst, cur_burst)
                in_burst = True

        total = len(seqs) + lost
        loss_pct = (lost / total * 100.0) if total > 0 else 0.0
        return lost,round(loss_pct, 4), max_burst, burst_count
    def throughput_kbps(self) -> dict:
        with self._lock:
            log = list(self.bytes_log)
        if len(log) < 2:
            return {}
        
        buckets =[]
        bkt_start = log[0][0]
        bkt_bytes = 0
        for t,b in log:
            bkt_bytes += b
            if t - bkt_start >= 1.0:
                buckets.append(bkt_bytes * 8 / (t - bkt_start) / 1000.0)
                bkt_bytes = 0
                bkt_start = t

        if not buckets:
            return {}
        std = statistics.stdev(buckets) if len(buckets) > 1 else 0.0
        return {
            "mean_kbps":round(statistics.mean(buckets),2),
            "min_kbps":round(min(buckets),2),
            "max_kbps":round(max(buckets),2),
            "stdev_kbps": round(std,2),
            "buckets": buckets,
        }
    def current_kbps(self) -> float:
        with self._lock:
            log = list(self.bytes_log)
        if len(log) < 2:
            return 0.0
        bkt_start = log[-1][0] - 1.0
        bkt_bytes = sum(b for t, b in log if t >= bkt_start)
        elapsed = log[-1][0] - max(log[0][0], bkt_start)
        if elapsed <= 0:
            return 0.0
        return bkt_bytes*8/elapsed/1000.0

def echo_listener_process(echo_port: int, bind_ip: str, rtt_queue: multiprocessing.Queue, stop_event: multiprocessing.Event):
    echo_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    echo_sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 * 1024 * 1024)
    echo_sock.bind((bind_ip, echo_port))
    echo_sock.settimeout(1.0)

    while not stop_event.is_set():
        try:
            data, _ = echo_sock.recvfrom(65535)
        except socket.timeout:
            continue
        except OSError:
            break
        
        arrival = hires_time()
        _, send_time, rtp_seq, rtp_ts, pt = extract_send_info(data)
        if send_time is not None:
            rtt_ms = (arrival - send_time) * 1000
            if 0.0 < rtt_ms < 60_000.0:
                rtt_queue.put({
                    'rtt_ms':   rtt_ms,
                    'rtp_seq':  rtp_seq,
                    'rtp_ts':   rtp_ts,
                    'pt':       pt,
                    'raw_len':  len(data),
                    'arrival':  arrival,
                })
    echo_sock.close()

# Forwarding thread

def forwarding_loop(loopback_sock: socket.socket, out_sock: socket.socket, dest: tuple, stop_event: threading.Event, send_metrics: SendMetrics):
    #Receive raw RTP from FFmpeg, inject timestamps, farward to receiver
    send_seq = 0
    sent = 0
    while not stop_event.is_set():
        try:
            data, _ = loopback_sock.recvfrom(65535)
        except socket.timeout:
            continue
        except OSError:
            break
        
        send_time = hires_time()
        stamped   = inject_timestamp(data, send_seq, send_time)
        out_sock.sendto(stamped, dest)
        send_metrics.record(send_time, len(stamped))
        send_seq = (send_seq + 1) & 0xFFFF_FFFF
        sent += 1

        if sent % 500 == 0:
            print(f"[Sender] Forwarded {sent} packets", flush = True)

# Helpers

def stat_line(values) -> str:
    if not values:
        return 'n/a'
    std = statistics.stdev(values) if len(values) > 1 else 0.0
    return (f"mean={statistics.mean(values):.3f}  "
            f"min={min(values):.3f}  "
            f"max={max(values):.3f}  "
            f"std={std:.3f} ms")

def save_json(all_rtts: list, metrics: StreamMetrics, send_metrics: SendMetrics, path: str, label: str, duration: float):
    lost, loss_pct, max_burst, burst_count = metrics.loss_and_bursts()

    sorted_rtts = sorted(all_rtts)
    def percentile(p):
        if not sorted_rtts:
            return None
        k = (len(sorted_rtts) - 1) *p / 100
        lo, hi = int(k),min(int(k) + 1, len (sorted_rtts) - 1)
        return round(sorted_rtts[lo] + (sorted_rtts[hi] - sorted_rtts[lo]) * (k - lo), 4)
    
    tx = send_metrics.throughput_kbps()
    rx = metrics.throughput_kbps()

    tx_buckets = tx.get("buckets", [])
    rx_buckets = rx.get("buckets", [])
    n = min(len(tx_buckets), len(rx_buckets))
    diff_buckets = [round(tx_buckets[i] - rx_buckets[i], 4) for i in range (n)]
    avg_diff = round(statistics.mean(diff_buckets), 4) if diff_buckets else None
    tx_out = {k: v for k, v in tx.items() if k != "buckets"}
    rx_out = {k: v for k, v in rx.items() if k != "buckets"}
    
    result = {
        "label":      label,
        "duration_s": round(duration, 3),
        "rtt": {
            "samples":   len(all_rtts),
            "mean_ms":   round(statistics.mean(all_rtts),   4) if all_rtts else None,
            "median_ms": round(statistics.median(all_rtts), 4) if all_rtts else None,
            "min_ms":    round(min(all_rtts),               4) if all_rtts else None,
            "max_ms":    round(max(all_rtts),               4) if all_rtts else None,
            "stdev_ms":  round(statistics.stdev(all_rtts),  4) if len(all_rtts) > 1 else None,
            "p50_ms":    percentile(50),
            "p95_ms":    percentile(95),
            "p99_ms":    percentile(99),
        },
        "jitter_ms": round(metrics.jitter_ms(), 4),
        "packet_loss": {
            "lost":        lost,
            "loss_pct":    loss_pct,
            "burst_count": burst_count,
            "max_burst":   max_burst,
        },
        "throughput_sent_kbps":     tx_out,
        "throughput_received_kbps": rx_out,
        "throughput_diff": {
            "avg_diff_kbps": avg_diff,
            "note": "sent minus received, per 1-second bucket average"
        },
        "samples": [round(r, 4) for r in all_rtts],
    }
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(result, f, indent=2)
    print(f" Results ({len(all_rtts)} RTT samples) saved to {path}")

# Main

def main():
    parser = argparse.ArgumentParser(
        description='RTP sender - round-trip-time measurement (Windows)'
    )
    parser.add_argument('--test', required=True, choices=['1080p', '4k', 'voice'], help = 'Test scenario')
    parser.add_argument('--dest', default='192.168.20.10', help='Receiver IP (default: 192.168.20.10)')
    parser.add_argument('--output', default='rtt_results.json', help = ' JSON output path')
    parser.add_argument('--label', default='', help='Label for this run')
    parser.add_argument('--dry', action='store_true', help= 'Print FFmpeg command and exit')
    parser.add_argument('--bind', default='0.0.0.0', help ='Local IP for echo socket (use VPN tunnel IP when on VPN)')
    args = parser.parse_args()

    label = args.label or args.test
    check_ffmpeg()
    
    ffmpeg_cmd, dest_port = build_ffmpeg_cmd(args.test)
    dest = (args.dest, dest_port)
    echo_port = dest_port + 1

    
    print(f"\n[Sender] Test           : {args.test}")
    print(f"[Sender] Destination    : {args.dest}:{dest_port}")
    print(f"[Sender] Echo-back port : {echo_port}  (receiver returns packets here)")
    print(f"[Sender] FFmpeg cmd     :\n  {' '.join(ffmpeg_cmd)}\n")

    if args.dry:
        print("[Sender] --dry set. Exiting.")
        return
    
    loopback_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    loopback_sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 * 1024 * 1024)
    loopback_sock.bind(('127.0.0.1', LOOPBACK_PORT))
    loopback_sock.settimeout(1.0)

    out_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    out_sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 8 * 1024 * 1024)

    rtt_queue = multiprocessing.Queue()
    mp_stop = multiprocessing.Event()
    echo_proc = multiprocessing.Process(
        target = echo_listener_process,
        args =(echo_port, args.bind, rtt_queue, mp_stop),
        daemon=True,
        )
    echo_proc.start()

    rtt_store = RttStore()
    metrics = StreamMetrics()
    send_metrics = SendMetrics()
    stop_event = threading.Event()

    fwd_thread = threading.Thread(
        target=forwarding_loop,
        args=(loopback_sock, out_sock, dest, stop_event,send_metrics),
        daemon=True
    )

    fwd_thread.start()

    print("[Sender] threads started. Launching FFmpeg... (60 s, Ctrl+C to stop early)\n")
    print(f"  {'Elapsed':>7}  {'Echoes':>7}  {'RTT avg':>10}"
          f" {'TX kbps':>10}  {'RX kbps':>10}  {'Diff':>10}")
    print(f"   {'-'*7} {'-'*7} {'-'*10} {'-'*10} {'-'*10} {'-'*10}")

    start_time = hires_time()
    last_print = [start_time]

    def status_printer():
        while not stop_event.is_set():
            while not rtt_queue.empty():
                try:
                    item = rtt_queue.get_nowait()
                    rtt_store.add(item['rtt_ms'])
                    metrics.update(
                        seq = item['rtp_seq'],
                        rtp_ts = item['rtp_ts'],
                        arrival = item['arrival'],
                        raw_len = item['raw_len'],
                        clock_rate = clock_rate_for(item['pt']),
                    )
                except Exception:
                    pass
            time.sleep(0.2)
            now = hires_time()
            if now - last_print[0] >= 1.0:
                elapsed = now - start_time
                all_rtts = rtt_store.snapshot()
                tx_kbps = send_metrics.current_kbps()
                rx_kbps = metrics.current_kbps()
                diff = tx_kbps - rx_kbps
                if all_rtts:
                    recent = all_rtts[-200:]
                    avg = statistics.mean(recent)
                    print(f"  {elapsed:>6.1f}s  {len(all_rtts):>7}  "
                          f"{avg:>9.3f}ms  "
                          f"{tx_kbps:>9.1f}  {rx_kbps:>9.1f}  {diff:>+9.1f}")
                else:
                    print(f"  {elapsed:>6.1f}s  {'0':>7}  {'(waiting for echo...)':>10}  "
                          f"{tx_kbps:>9.1f}  {'---':>10}  {'---':>10}")
                last_print[0] = now
    threading.Thread(target=status_printer, daemon=True).start()

    try:
        proc = subprocess.run(ffmpeg_cmd)
        if proc.returncode not in (0,1,255):
            print(f"\n[Sender] FFmpeg exited with code {proc.returncode}")
    except KeyboardInterrupt:
        print("\n[Sender] Interrupted by user.")
    finally:
        stop_event.set()
        mp_stop.set()
        fwd_thread.join(timeout=2)
        echo_proc.join(timeout=3)
        loopback_sock.close()
        out_sock.close()

        while not rtt_queue.empty():
            try:
                item = rtt_queue.get_nowait()
                rtt_store.add(item['rtt_ms'])
                metrics.update(
                     seq = item['rtp_seq'],
                    rtp_ts = item['rtp_ts'],
                    arrival= item['arrival'],
                    raw_len = item['raw_len'],
                    clock_rate = clock_rate_for(item['pt']),
                )
            except Exception:
                pass
        
        duration = hires_time() - start_time
        all_rtts = rtt_store.snapshot()

        tx = send_metrics.throughput_kbps()
        rx = metrics.throughput_kbps()
        tx_buckets = tx.get("buckets", [])
        rx_buckets = rx.get("buckets", [])
        n = min(len(tx_buckets), len(rx_buckets))
        diff_buckets = [tx_buckets[i] - rx_buckets[i] for i in range(n)]
        avg_diff = statistics.mean(diff_buckets) if diff_buckets else None 
        
        print()
        print("=" * 60)
        print (f"  FINAL RTT RESULTS  -  {label}  -  {duration:.1f}s")
        print("=" * 60)
        if all_rtts:
            print(f"  Samples         : {len(all_rtts)}")
            print(f"  RTT             : {stat_line(all_rtts)}")
            print(f"  One-way est.    : ~{statistics.mean(all_rtts)/2:.3f} ms  (RTT ÷ 2)")
        else:
            print("  No echo packets received.")
            print(f"  → Is receiver.py running on {args.dest}?")
            print(f"  → Is its firewall open for UDP {dest_port} (in) and {echo_port} (out)?")
        print()
        print("  THROUGHPUT SUMMARY  (RTP-layer, kbps)")
        print(f"  {'':20}  {'Mean':>10}  {'Min':>10}  {'Max':>10}  {'Stdev':>10}")
        print(f"  {'-'*20}  {'-'*10}  {'-'*10}  {'-'*10}  {'-'*10}")
        if tx:
            print(f"  {'Sent (TX)':20}  {tx['mean_kbps']:>10.1f}  "
                  f"{tx['min_kbps']:>10.1f}  {tx['max_kbps']:>10.1f}  "
                  f"{tx['stdev_kbps']:>10.1f}")
        if rx:
            print(f"  {'Received (RX)':20}  {rx['mean_kbps']:>10.1f}  "
                  f"{rx['min_kbps']:>10.1f}  {rx['max_kbps']:>10.1f}  "
                  f"{rx['stdev_kbps']:>10.1f}")
        if avg_diff is not None:
            print(f"\n  Avg TX−RX diff  : {avg_diff:+.2f} kbps  "
                  f"({'TX higher — packets dropped in network' if avg_diff > 0 else 'RX higher — timing artefact'})")
        else:
            print("\n  Avg TX−RX diff  :n/a (insufficient data)")
        print()

        if all_rtts:
            save_json(all_rtts, metrics, send_metrics, args.output, label, duration)

        print("[Sender] Done.")


if __name__ == '__main__':
    multiprocessing.freeze_support()
    main()
