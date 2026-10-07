# Security and public-release gate

The Pool is for trusted devices on a personal, local Ethernet cable or switch.
Its transport needs neither internet access nor Bluetooth. It does not encrypt
traffic or give individual agents separate identities. Read the README security
section before enabling networking or notifications.

## Compatible preparation

This change preserves the existing key, wire format, ports, names, commands,
bookmarks, book and notification configuration. It prefers an ignored private
configuration and still accepts the legacy tracked configuration. It does not
deploy itself, restart a running Pool, change firewall rules on its own, or alter
history. Updated Windows setup narrows future inbound allow rules to wired
interfaces; running setup is an operator action.

On each active device after updating to this preparation version:

1. Run `./pool migrate-config` (Linux/Git Bash) or `pool.bat migrate-config`
   (Windows). This creates `pool_config.local.json` with the exact same key and
   names and never overwrites an existing file. Keep this file private and verify
   it is ignored by Git. A previously configured `POOL_CONFIG_PATH` takes priority.
2. Confirm normal direct and pool messaging during a planned check. Do not send
   the configuration or key as a Pool message or attachment.
3. Inventory every active device and any optional outage watcher. The watcher
   on `outage-rebirth` directly reads the legacy filename and needs a separate,
   reviewed adaptation before that file is removed. Do not switch branch users
   to main or delete their configuration automatically.

No key rotation happens in these steps. That is deliberate to keep current
members connected. The history remains sensitive, so keep this repo private.

## Before making any repository public

1. Schedule a coordinated key change with all active devices. Generate a new
   random key off Git and privately distribute it. Change and restart the Pool
   on each device together; mixed old/new keys cannot communicate. Verify normal
   messaging and notification delivery, then retire every use of the old key.
2. Remove tracked operational configuration from the public version and use only
   an empty example. Replace real hostnames, private addresses, personal paths,
   incident details and personal names in documentation and any other branches.
3. Sanitize all history and commit author/committer emails in a separate copy.
   Branches, tags, pull-request refs, attachments and release assets also count.
   Changing visibility publishes other branches too, not just main. Do not
   force-push over active users' history without an agreed migration plan.
4. Scan the final public candidate's complete history for the retired key and
   personal information. Confirm no local config, book, received file, peer
   database, hook config or log is included. A custom Pool key may not match
   GitHub's built-in provider-secret patterns.
5. To preserve current users' Git history, prefer a **new, clean public repository**
   containing the sanitized source snapshot, while keeping this existing private
   repository private. Current members can continue using their original remote.
   If this exact repository must become public, history cleanup requires an
   explicitly coordinated migration for every checkout and branch.
6. Verify firewall scope on every device and confirm that no router forwarding,
   tunnel or relay exposes the configured ports. This cannot be inferred from
   source code alone. On Linux restrict both ports to the physical Ethernet
   interface and its directly connected subnet; on Windows inspect the installed
   Pool rules after a planned setup run.

Only after these checks pass is the public-release gate satisfied. Preparing or
merging the compatible hardening change alone does not satisfy it.

GitHub documents why [removing secrets requires rotation and coordination](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/removing-sensitive-data-from-a-repository).

## Reporting a problem

Do not post a key, message history, addresses or private paths in a public issue.
Report privately to the repository owner. Do not probe other people's Pool
devices or attempt to wake their agents while reporting a problem.
