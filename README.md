# GGR Vacations 0.2.0

Archives des **vacations HF** entre le radio-club **F6KUF** et les bateaux de la flotte **Golden Globe Race**.

Tous les jours à **18:00 TU**, F6KUF émet un bulletin météo sur **14.135 MHz USB** (QRG nominale ± 5 kHz, suivi automatique si QRM) et écoute les accusés de réception sur **16.551 MHz USB** et **12.418 MHz USB** (± 5 kHz). Tous les jours à **12:00 TU**, un **buddy call** est écouté sur **4483 kHz USB** (principale) et **6516 kHz USB** (secours). L’application choisit les [KiwiSDR](http://kiwisdr.com/) selon la position : **bulletin 14 MHz** sur le récepteur le plus proche de la flotte ; **ACK** en parallèle près des bateaux, **en France** et **vers Tahiti** ; **buddy call** sur plusieurs récepteurs en NVIS et en saut 1 hop vers le centroïde des skippers suivis (défaut : Damien Guillou, Etienne Messikommer, Louis Kerdelhue). Audio USB + **screencast** de l’interface SDR pour le replay.

Les trois QRG, la tolérance, l’avance et la durée se règlent dans l’UI (**Réglages**). Un **record immédiat** permet de tester le suivi ± 5 kHz sans attendre 18:00 TU.

## Fonctionnement

1. **Flotte** — centroïde des bateaux en course via le tracker Yellowbrick (`/BIN/ggr2026/AllPositions3`). Buddy call : centroïde d’un sous-ensemble de skippers (trio par défaut, ou trio + flotte).
2. **SDR** — bulletin : Kiwi le plus proche de la flotte ; ACK : un Kiwi près de la flotte, un en France (≤ 1500 km des Sables-d’Olonne), un vers Tahiti / Papeete (≤ 2500 km). Buddy : plusieurs Kiwi qui couvrent **4483** et **6516 kHz**, NVIS proche + saut 1 hop (~1400–3200 km à 12:00 TU), pas seulement le plus proche.
3. **Enregistrement** — 1 minute avant 18:00 TU, pendant 10 minutes (configurable) :
   - chasse USB autour de **14.135 MHz** (± 5 kHz) puis screencast Playwright ;
   - WAV 12 kHz sur le bulletin **et** les deux QRG d’accusé **en même temps**, aux trois sites ;
   - muxage ffmpeg → MP4 H.264 / AAC.
   Buddy call : 1 minute avant **12:00 TU**, 15 minutes, **4483 kHz** + **6516 kHz** en parallèle.
4. **Replay** — interface web (français) : liste des vacations, lecteur vidéo, pistes audio.

Déploiement prévu sur un VPS **Ubuntu 26.04** avec **k3s**, dans le namespace **`ggr-vacations`**. Un seul pod sert l’UI et lance l’enregistreur (APScheduler). Le volume des archives est un PVC **5 Gio** (`local-path`, donc sur `/`) : adapté à un disque racine d’une soixantaine de Go.

## Mise à jour serveur (k3s)

```bash
cd /opt/ggr-vacations
./scripts/update.sh local     # sudo demandé pour Docker et k3s
```

Ensuite, après un `git push` sur `main` :

```bash
cd /opt/ggr-vacations
sudo ./scripts/update.sh           # tire GHCR si origin GitHub, sinon rebuild
```

L’UI est en **HTTPS** via Traefik + Let’s Encrypt : [https://ggr-vacations.k3s.lpb.ovh](https://ggr-vacations.k3s.lpb.ovh).  
Le NodePort `http://<IP-du-VPS>:30080` reste disponible en secours.

Test scan 20 m (environ 2,5 min, USB 14,19–14,275 MHz) après déploiement de cette version :

```bash
sudo k3s kubectl -n ggr-vacations exec deploy/ggr-vacations -- python -m recorder.session --test-20m
```

La carte **Test scan 20 m** apparaît ensuite sur l’accueil.

```bash
sudo k3s kubectl -n ggr-vacations get pods,svc,pvc
```

Le nom DNS est `ggr-vacations.k3s.lpb.ovh` (`k8s/ingress.yaml`). Traefik (k3s) termine le TLS ; cert-manager renouvelle le certificat.

Enregistrement manuel (page **Réglages** → *Record cette QRG*, ou jeton `GGR_ADMIN_TOKEN`) :

```bash
# Test sur une QRG libre (14.135 MHz ou 14135 kHz, suivi ± 5 kHz)
curl -X POST -H "X-Admin-Token: …" \
  -H "Content-Type: application/json" \
  -d '{"freq_khz": 14.135, "duration_minutes": 2, "hunt": true}' \
  https://ggr-vacations.k3s.lpb.ovh/api/vacations/record

# Vacation complète (bulletin + ACK flotte / France / Tahiti)
curl -X POST -H "X-Admin-Token: …" \
  -H "Content-Type: application/json" \
  -d '{"duration_minutes": 10}' \
  https://ggr-vacations.k3s.lpb.ovh/api/vacations/record

# Buddy call immédiat (4483 / 6516 kHz)
curl -X POST -H "X-Admin-Token: …" \
  -H "Content-Type: application/json" \
  -d '{"kind": "buddy", "duration_minutes": 15}' \
  https://ggr-vacations.k3s.lpb.ovh/api/vacations/record
```

Les QRG survivent au redéploiement (fichier `/data/settings.json` sur le PVC). Le ConfigMap k3s reste le défaut.

## Configuration radio

Défauts dans [`config/default.yaml`](config/default.yaml) ; overrides runtime dans **Réglages**.

| Paramètre | Valeur |
| --- | --- |
| Bulletin | 14.135 MHz USB, ± 5 kHz, 18:00 TU |
| Accusé | 16.551 MHz USB, 12.418 MHz USB (± 5 kHz ; flotte + France + Tahiti, en parallèle du bulletin) |
| Buddy call | 4483 kHz USB (principale), 6516 kHz USB (secours), 12:00 TU, 15 min |
| Centroïde buddy | Damien Guillou, Etienne Messikommer, Louis Kerdelhue (modifiable ; option « + flotte ») |
| Avance | 1 min (début 17:59 TU) |
| Durée | 10 min (fin 18:09 TU) |
| Tracker | `ggr2026` sur `cf.yb.tl` |
| Rétention | 14 jours (PVC 5 Gio) |

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
