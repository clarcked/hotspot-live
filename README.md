# Wifi

device: tp-link archer tx20uh

## Clients

- `./app.py clients list` - connected clients (MAC, IP, host, signal, total traffic)
- `./app.py clients monitor [--interval 2]` - live table with per-client down/up rates
- `sudo ./app.py clients block|unblock <MAC>` - drop a client (iptables) and try to deauthenticate it

Hostnames need read access to the NetworkManager dnsmasq leases (try with sudo).
