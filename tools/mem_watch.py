#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mem_watch.py — 长时内存/稳定性监控（64MB 小板专用）。

每轮采样：uptime / MemAvailable / dmesg 里的 panic+OOM+UBIFS 错误计数 / majestic pid。
设备一旦重启，uptime 归零会被识别并标红。

用法:
    mem_watch.py [轮数] [间隔秒]      默认 30 轮 / 60 秒
日志: .tmp/mem_watch.log
"""
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable
DEVSSH = os.path.join(HERE, 'tools', 'devssh.py')
LOG = os.path.join(HERE, '.tmp', 'mem_watch.log')

CHECK = ("uptime | awk '{print $3,$4}'; "
         "grep -a MemAvailable /proc/meminfo | awk '{print $2}'; "
         "dmesg 2>/dev/null | grep -aicE 'panic|Unable to handle|Out of memory'; "
         "dmesg 2>/dev/null | grep -aicE 'ubifs error|bad CRC'; "
         "pidof majestic | head -1")


def run(cmd, timeout=40):
    try:
        r = subprocess.run([PY, DEVSSH, 'run', cmd], capture_output=True,
                           timeout=timeout, cwd=HERE)
        return r.stdout.decode('latin1', 'replace')
    except Exception as e:
        return 'ERR:%s' % type(e).__name__


def parse(out):
    lines = [l.strip() for l in out.splitlines() if l.strip()]
    vals = []
    for l in lines:
        if l.startswith('[') or l.startswith('rc=') or 'ERR' in l[:12]:
            continue
        vals.append(l)
    return vals


def main():
    rounds = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    gap = int(sys.argv[2]) if len(sys.argv) > 2 else 60
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    prev_up = None
    for i in range(rounds):
        t = time.strftime('%H:%M:%S')
        out = run(CHECK)
        v = parse(out)
        up = v[0] if len(v) > 0 else '?'
        mem = v[1] if len(v) > 1 else '?'
        panic = v[2] if len(v) > 2 else '?'
        ubifs = v[3] if len(v) > 3 else '?'
        pid = v[4] if len(v) > 4 else '?'
        flag = ''
        try:
            if prev_up is not None and int(str(up).split(',')[0].strip()) < int(str(prev_up).split(',')[0].strip()):
                flag = '  <<< REBOOTED'
        except Exception:
            pass
        if panic not in ('0', '?'):
            flag += '  <<< PANIC/OOM'
        if ubifs not in ('0', '?'):
            flag += '  <<< UBIFS_ERR'
        line = "%s #%02d up=%s mem=%skB panic=%s ubifs=%s maj=%s%s" % (
            t, i, up, mem, panic, ubifs, pid, flag)
        print(line, flush=True)
        with open(LOG, 'a', encoding='utf-8') as f:
            f.write(line + '\n')
        prev_up = up
        if i + 1 < rounds:
            time.sleep(gap)


if __name__ == '__main__':
    main()
