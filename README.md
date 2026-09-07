# GGR Vacations 0.1.0

Archives des **vacations HF** entre le radio-club **F6KUF** et les bateaux de la flotte **Golden Globe Race**.

Tous les jours à **18:00 TU**, F6KUF émet un bulletin météo sur **14.135 MHz USB** (QRG nominale ± 2 à 3 kHz) et écoute les accusés de réception sur **16.5515 MHz USB** et **12.4185 MHz USB**. L’application choisit les meilleurs [KiwiSDR](http://kiwisdr.com/) selon la position moyenne de la flotte, enregistre l’audio USB et capture un **screencast** de l’interface SDR (waterfall / VFO) pour le rejouer ensuite comme si l’on était devant le récepteur.

## Fonctionnement

1. **Flotte** — centroïde des bateaux en course via le tracker Yellowbrick (`/BIN/ggr2026/AllPositions3`).
2. **SDR** — classement des KiwiSDR (distance à la flotte, SNR HF, places libres, couverture 12–17 MHz).
3. **Enregistrement** — 10 minutes avant 18:00 TU, pendant 45 minutes (configurable) :
   - screencast Playwright du Kiwi accordé sur **14.135 MHz USB** ;
   - WAV 12 kHz sur le bulletin et les deux QRG d’accusé ;
   - muxage ffmpeg → MP4 H.264 / AAC.
4. **Replay** — interface web (français) : liste des vacations, lecteur vidéo, pistes audio.

Déploiement prévu sur un VPS **Ubuntu 26.04** avec **k3s**. Un seul pod sert l’UI et lance l’enregistreur (APScheduler).

## Mise à jour serveur (k3s)

```bash
sudo git clone https://github.com/<compte>/ggr-vacations.git /opt/ggr-vacations
cd /opt/ggr-vacations
sudo ./scripts/update.sh local     # premier déploiement : build Docker + import k3s
```

Ensuite, après un `git push` sur `main` :

```bash
cd /opt/ggr-vacations
sudo ./scripts/update.sh           # tire GHCR si origin GitHub, sinon rebuild
```

L’UI est exposée en **NodePort 30080** : `http://<IP-du-VPS>:30080`.

Pour un nom DNS, modifier `k8s/ingress.yaml` (`ggr-vacations.local`) puis relancer `./scripts/update.sh`. Traefik (fourni par k3s) prend l’Ingress en charge.

Enregistrement manuel (jeton `web.admin_token` ou variable `GGR_ADMIN_TOKEN`) :

```bash
curl -X POST -H "X-Admin-Token: …" http://<IP>:30080/api/vacations/record
```

## Configuration radio

Fichier unique : [`config/default.yaml`](config/default.yaml) (monté en ConfigMap k3s).

| Paramètre | Valeur |
| --- | --- |
| Bulletin | 14.135 MHz USB, ± 3 kHz, 18:00 TU |
| Accusé | 16.5515 MHz USB, 12.4185 MHz USB |
| Avance | 10 min |
| Durée | 45 min |
| Tracker | `ggr2026` sur `cf.yb.tl` |
| Rétention | 180 jours |

## Développement local

Python 3.12+, ffmpeg, et Chromium Playwright :

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
playwright install chromium
make test
make run          # http://127.0.0.1:8080
```

Docker Compose :

```bash
docker compose up --build
```

## Licence

MIT. Crédits : F6KUF, Golden Globe Race / Yellowbrick, opérateurs KiwiSDR, liste rx.linkfanel.net.
