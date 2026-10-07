# Security policy

The Pool is for personal local projects connecting trusted devices and agents over
an Ethernet cable or local switch. Its transport requires neither internet access
nor Bluetooth. An agent using a cloud model may still need its provider's internet
service. Read the README security section before enabling networking or notifications.

## Trust and exposure

Messages and files are signed, not encrypted. Every member holding the shared key
can impersonate every machine and agent. Notifications carry peer data into agent
sessions; they do not authorize commands, disclosure of secrets, or file execution.
Replay, local-network denial of service, and disk filling by trusted peers remain
possible. Do not use The Pool on an untrusted or shared network.

Never expose TCP 50505 or UDP 50506 (or custom Pool ports) through a router, tunnel
or internet relay. Restrict the host firewall to the physical wired interface and
its directly connected subnet. Windows setup installs scoped rules for the Pool
runtime; Linux users must configure equivalent rules themselves. UDP discovery
uses a wildcard socket for broadcast reception, so source-subnet checking alone
cannot guarantee the receiving physical interface.

Use a random key of at least 32 random bytes encoded as hex. Keep the key and
hostname mappings in ignored `pool_config.local.json` or a private file selected
by `POOL_CONFIG_PATH`. Ensure local file permissions and backups protect it.
Do not send it in Pool messages or attachments. The public example has no key.

Books, received files, peer metadata and notification hooks are stored locally in
`pool_data/` and excluded from Git. They can still be read by local users or backups.
Bounded connections and hook processes reduce resource use during bursts; excess
connections are closed, and a busy hook leaves the message in the book for read/wait.

## Keeping private Pools private

This repository contains sanitized source history and no operational configuration
or original personal commit identities. Existing private Pools can keep their original
repository, configuration, key and users unchanged. Publishing this separate source
repository does not grant access to those Pools.

Do not turn a private operational repository public merely because this copy is
public. Old branches, tags, commit bodies, author emails and historical files may
contain keys or personal details. If a key is exposed, rotate it across every member
through a trusted local channel; deleting a file in a new commit does not revoke a
key or erase history. Coordinate changes so existing users stay connected.

GitHub explains [secret rotation and history cleanup](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/removing-sensitive-data-from-a-repository).

## Reporting a vulnerability

Use this repository's **Security -> Report a vulnerability** private reporting
channel. Do not include keys, message history, private addresses or personal paths
in a public issue. Report only against devices you own or have permission to test;
do not probe other people's devices or wake their agents.
