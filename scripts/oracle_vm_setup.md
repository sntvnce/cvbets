# Oracle Cloud VM setup — what to click

Your SSH keys are already created on this Mac:
- Private key: ~/.ssh/oracle_vm          (never share, never paste anywhere)
- Public key:  ~/.ssh/oracle_vm.pub      (this is the "lock" you give Oracle)

Public key to paste into Oracle (starts with ssh-ed25519):
ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIN350Ymhe9yxM4BqgEGHF/KcYwy1xDmkL236Yx9Z4laM sntvnce@cvbets-cloud

## In the Oracle console (cloud.oracle.com):

1. Menu (hamburger, top-left) -> Compute -> Instances -> "Create Instance"
2. Name: cvbets-bot
3. Image: Ubuntu 24.04 (click "Change image" if it's not Ubuntu)
4. Shape: click "Change shape" -> Ampere -> VM.Standard.A1.Flex
   -> 2 OCPUs, 12 GB memory (the Always Free ARM box)
5. Networking: leave defaults, BUT make sure
   "Assign a public IPv4 address" is checked
6. SSH keys section: choose "Paste a public key"
   -> paste the ssh-ed25519 line above
7. Create. Wait ~1 minute for state RUNNING.
8. Copy the "Public IP address" shown on the instance page.

## Then tell me two things:
   1. The public IP
   2. Your home region (shows top-right of the console, e.g. us-ashburn-1)

## If you hit "Out of host capacity":
   - Try 1 OCPU / 6 GB instead of 2/12 (free tier allows 2 instances of 1 OCPU)
   - Or try again later / different availability domain — capacity frees in bursts
   - Do NOT give up the account over this; it's the most common Oracle quirk

## After you give me the IP, I will:
   - SSH in with your private key (no password involved)
   - Install Hermes + move the Discord gateway there
   - Set up auto-restart so the bot survives VM reboots
   - Hand you back a running cloud bot, laptop-free