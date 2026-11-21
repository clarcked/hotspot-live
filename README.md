# Wifi

device: tp-link archer tx20uh

## Clients

- `./holafi.py clients list` - connected clients (MAC, IP, host, signal, total traffic)
- `./holafi.py clients monitor [--interval 2]` - live table with per-client down/up rates
- `sudo ./holafi.py clients block|unblock <MAC>` - drop a client (iptables) and try to deauthenticate it

Hostnames need read access to the NetworkManager dnsmasq leases (try with sudo).
