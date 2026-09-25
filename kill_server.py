import psutil

try:
    killed = 0
    for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
        try:
            if proc.info['name'] == 'python.exe':
                cmdline_list = proc.info.get('cmdline')
                if cmdline_list:
                    cmdline = " ".join(cmdline_list)
                    if 'server.py' in cmdline:
                        proc.kill()
                        killed += 1
                        print(f"Killed stale server.py PID: {proc.pid}")
        except Exception:
            pass
    print(f"Total killed: {killed}")
except Exception as e:
    print(f"Fatal error string: {e}")
