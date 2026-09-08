# Hetzner VM setup — what to click (replaces the Oracle guide)

Your SSH keys are already created on this Mac:
- Private key: ~/.ssh/oracle_vm          (never share — reused for Hetzner, same key works)
- Public key:  ~/.ssh/oracle_vm.pub      (paste into Hetzner)

Public key to paste (starts with ssh-ed25519):
ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIN350Ymhe9yxM4BqgEGHF/KcYwy1xDmkL236Yx9Z4laM sntvnce@cvbets-cloud

## Sign up
1. https://console.hetzner.cloud -> sign up (email + card or PayPal)
2. New project (any name, e.g. "cvbets") -> enter it

## Create the server (big blue "+ New Server")
- Location:  Ashburn, VA (us-east) — closest to you; Germany is ~EUR 0.50 cheaper
- Image:     Ubuntu 24.04
- Type:      Shared vCPU x86, cheapest plan (CX22 or CX23, ~EUR 4.35-5.49/mo
             + ~EUR 0.50/mo for the IPv4 address). Skip ARM (CAX) and dedicated (CCX).
- SSH Key:   "+ Add key" -> paste the ssh-ed25519 line above -> it validates instantly
- Volumes / Firewalls / Backups: leave all defaults (backups optional, +20%, skip)
- Server name: cvbets-bot
- "Create & Buy Now" — RUNNING in ~10 seconds, ~EUR 5-6/mo total

## Then tell me:
   1. The public IPv4 address (on the server page, after creation)

## What I do after that (no more clicking for you):
   - SSH in with your private key (root@IP — Hetzner hands root to SSH keys)
   - Install Hermes + deps, move the Discord gateway from the Mac to the VM
   - Systemd auto-restart so the bot survives reboots
   - Verify the bot answers in Discord, then retire the local gateway
   - Optional: migrate Hermes sessions/memory too, or keep CLI sessions local

## Cost sanity: ~EUR 5-6/mo (~$6-7) billed monthly by the hour; cancel anytime.
   The data pipeline stays on GitHub Actions ($0) — the VM only runs the chat bot.