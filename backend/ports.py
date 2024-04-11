import psutil

class PortSelector:
    def __init__(self):
        self.current_port = 9999

    def __iter__(self):
        return self

    def __next__(self):
        self.current_port += 1
        if self.current_port > 20000:
            self.current_port = 10000
        return self.current_port


# Old code, this sometimes actually failed..

#def get_next_open_port(start=10000, end=20000):
#    import psutil
#    # This is kind of bad I know :)
#    used_ports = []
#    for conn in psutil.net_connections(kind="inet4"):
#        if conn.status == 'LISTEN' and conn.laddr.port >= start and conn.laddr.port <= end:
#            used_ports.append(conn.laddr.port)
#
#    r = next(rng for rng in iter(lambda: random.randint(start, end), None) if rng not in used_ports)
#
#    return r
