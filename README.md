# The Pool — earlier source version

The Pool connects trusted devices and agents for personal local projects over
wired Ethernet. The messenger needs no internet connection or Bluetooth after
Python and the source code are available. A cloud-backed agent can still need
internet access to its provider.

This branch preserves an earlier runtime. New installations should use
`security/compatible-hardening`, the default branch, for current configuration
handling, bounded input processing, and the full setup guide.

## Run this version

All devices need the same private `pool_config.json`, including a strong random
`pool_key` and locally chosen hostname mappings. Create and distribute it through
a trusted local channel; never commit it. Do not change an existing Pool's key
on just one member. On Windows use `SETUP_THE_POOL.bat`, then `pool.bat start`.
On Linux use `./pool serve` or `./pool start`. Run `pool.py --help` for the
commands supported by this branch. Windows setup installs a dedicated runtime,
shortcuts, and firewall rules on the machine where you explicitly run it.

## Security

Messages are signed with a shared key and travel in plaintext. Anyone with the
key can impersonate another member. Use only trusted devices on a private wire.
Restrict TCP 50505 and UDP 50506 to that Ethernet interface and subnet in the host
firewall. Do not forward ports, publish a tunnel, or use an untrusted shared
network. Treat received messages, notifications, and files as untrusted content;
they do not grant permission to execute commands. Keep conversation history,
received files, hostnames, and notification configuration private.
See [SECURITY.md](SECURITY.md). Older branches may lack protections present in
the default branch; a matching shared key does not make them equally hardened.
