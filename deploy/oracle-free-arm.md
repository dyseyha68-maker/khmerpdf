# Trying the app on Oracle Cloud Always Free (ARM)

Goal: run a second copy of the site on a free ARM server, test it, and only switch the domain
once it has been stable for a while. **Do not move production until then.**

Checked: every direct dependency in `requirements.txt` ships a prebuilt Linux aarch64 wheel (or is
pure Python), and `Dockerfile` / `docker-compose.yml` need no change on ARM.
Not checked: Oracle's current free-tier limits (reports say it was cut to 2 OCPU / 12 GB in 2026; see
Oracle's own Always Free page before relying on it).

## 1. Account (you)
- Sign up at oracle.com/cloud/free. A card is needed for identity verification.
- **Pick the home region carefully; it cannot be changed.** Choose the one closest to Cambodia that
  offers Ampere A1 (Singapore `ap-singapore-1`, or Tokyo / Seoul).
- Stay on the free tier. Some people upgrade to Pay-As-You-Go (still free inside the limits) so idle
  instances are less likely to be reclaimed. Check Oracle's current policy before deciding.

## 2. Create the server (you)
Compute -> Instances -> Create instance:
- **Image:** Canonical Ubuntu 24.04 (or 22.04), *aarch64*.
- **Shape:** Ampere -> `VM.Standard.A1.Flex`, **2 OCPU, 12 GB** (stay inside the free limit; the app needs about 4 GB).
- **Boot volume:** 50 GB (the minimum is about 47 GB; total free block storage is 200 GB).
- **Networking:** assign a public IPv4.
- **SSH key:** paste the public key below (it is the public half of `seyha.pem` in the project folder, so Claude can connect with the key it already has).
- If you get "Out of capacity": try another availability domain, or retry later / another day.

```
ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQDKZKsE4juMSmUeIWasPnLwXktpNauu2V3pbLNAu/v/UEyZfWhf8KEzH7+wc8FQorVy/PHoDospEYwpi3J5e+Z8NLs+jfZC1EWzgAQE5nsHJgvNjyGP0kLjQu8qKsuH0uGMs6ad/IMVfEch8K+4r8I8fqkRg37BE/vxf5MSZjkKIuCGNasUO8xbJKQgXlVfzQP4PpwslIWTjzFUxfJHyGz8srPBjTRpsg3Dmz5KFMPmkFc6OtZy7w4cMkAF1pA5N0mJTaidQgf7WpjQG50h4SPO98ed7DYpihqITr2Q58XhdrDkbon//WoTLX+9uRjJ2ODrj9H/nPLFy70zwr92DWSv
```

## 3. Open the firewall (you)
- In the console: Networking -> your VCN -> Security List (or NSG) -> add **ingress** rules for TCP **80** and **443** (SSH 22 exists).
- The Ubuntu image also blocks ports with `iptables`; `setup_server.sh` opens 80/443 and saves the rule.

## 4. Install the app (Claude or you)
```bash
ssh ubuntu@<PUBLIC_IP>
curl -fsSLO https://raw.githubusercontent.com/dyseyha68-maker/khmerpdf/master/setup_server.sh
sudo bash setup_server.sh      # installs Docker, nginx, certbot; clones the repo to /var/www/khmerpdf; builds and starts the containers
```
Notes:
- The script is written for `damspey.com`. For a test, browse to `http://<PUBLIC_IP>/` (the IP is allowed in `ALLOWED_HOSTS`).
- The first build compiles nothing, but it downloads packages and takes several minutes.
- Do **not** run `certbot` until the domain's DNS points at this server.

## 5. Copy the data from the current server
Only the SQLite database matters (it holds the OCR settings, including the saved API keys, ads,
translations and calendar). Uploaded files expire after 24 hours; the model cache re-downloads.

On the **current** server:
```bash
cd /var/www/khmerpdf
docker compose exec -T web python -c "import sqlite3; s=sqlite3.connect('/data/db.sqlite3'); d=sqlite3.connect('/data/backup.sqlite3'); s.backup(d); d.close()"
docker compose cp web:/data/backup.sqlite3 /tmp/backup.sqlite3
scp /tmp/backup.sqlite3 ubuntu@<NEW_IP>:/tmp/        # needs an SSH login to the new server from there
```
On the **new** server:
```bash
cd /var/www/khmerpdf
sudo docker compose stop web celery celery-ocr
sudo docker compose cp /tmp/backup.sqlite3 web:/data/db.sqlite3
sudo docker compose up -d
```

## 6. Test before switching
- All tool pages load; run one compress, one Khmer OCR (watch `sudo docker compose logs -f celery-ocr` for
  "Netra-OCR model loaded and ready"), one English OCR (needs Gemini enabled in the admin).
- Check memory with `free -m` and `sudo docker stats --no-stream`.
- Leave it running for a few weeks. Oracle may reclaim an instance that stays almost idle for a long
  time, so keep an eye on it (and keep the old server until you trust the new one).

## 7. Switching the domain
1. A day before: lower the DNS TTL of `damspey.com` / `www` to 300 seconds.
2. Point the A records at the new IP.
3. On the new server: `sudo certbot --nginx -d damspey.com -d www.damspey.com`.
4. Keep the old server for a week as a fallback (change the DNS back if needed).
