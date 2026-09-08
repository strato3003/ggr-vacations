#!/usr/bin/env bash
# Mise à jour du déploiement k3s depuis ce dépôt.
# Usage :
#   ./scripts/update.sh          # GHCR si origin GitHub, sinon build local
#   ./scripts/update.sh local    # build Docker + import k3s
#   ./scripts/update.sh pull     # image ghcr.io/<owner>/ggr-vacations:latest
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

KNS="ggr-vacations"
MODE="${1:-auto}"

as_root() {
  if [[ "$(id -u)" -eq 0 ]]; then
    "$@"
  else
    sudo "$@"
  fi
}

kc() {
  if command -v k3s >/dev/null 2>&1; then
    as_root k3s kubectl "$@"
  else
    kubectl "$@"
  fi
}

if [[ -d .git ]]; then
  git pull --ff-only || true
fi

# Kustomize n'autorise que les fichiers sous k8s/ (restriction de sécurité).
cp "$ROOT/config/default.yaml" "$ROOT/k8s/config.yaml"

image_from_origin() {
  local remote owner repo
  remote="$(git remote get-url origin 2>/dev/null || true)"
  if [[ "$remote" =~ github.com[:/]([^/]+)/([^/.]+) ]]; then
    owner="$(printf '%s' "${BASH_REMATCH[1]}" | tr '[:upper:]' '[:lower:]')"
    repo="$(printf '%s' "${BASH_REMATCH[2]}" | tr '[:upper:]' '[:lower:]')"
    printf 'ghcr.io/%s/%s:latest\n' "$owner" "$repo"
  fi
}

build_local() {
  local img="ggr-vacations:local"
  if ! command -v docker >/dev/null 2>&1; then
    echo "docker est requis pour le build local (ou utilisez : $0 pull)" >&2
    exit 1
  fi
  as_root docker build -t "$img" "$ROOT"
  as_root docker save "$img" | as_root k3s ctr images import -
  printf '%s\n' "$img"
}

IMAGE=""
case "$MODE" in
  local) IMAGE="$(build_local)" ;;
  pull)
    IMAGE="$(image_from_origin)"
    if [[ -z "$IMAGE" ]]; then
      echo "Impossible de déduire ghcr.io depuis git remote origin" >&2
      exit 1
    fi
    ;;
  auto)
    IMAGE="$(image_from_origin)"
    if [[ -z "$IMAGE" ]]; then
      IMAGE="$(build_local)"
    fi
    ;;
  *)
    echo "usage: $0 [auto|local|pull]" >&2
    exit 1
    ;;
esac

kc apply -f "$ROOT/k8s/namespace.yaml"
kc apply -k "$ROOT/k8s"
kc -n "$KNS" set image "deploy/ggr-vacations" "web=${IMAGE}"

if [[ "$IMAGE" == ghcr.io/* ]]; then
  kc -n "$KNS" patch deploy ggr-vacations --type json \
    -p '[{"op":"replace","path":"/spec/template/spec/containers/0/imagePullPolicy","value":"Always"}]'
fi

kc -n "$KNS" rollout restart "deploy/ggr-vacations"
kc -n "$KNS" rollout status "deploy/ggr-vacations" --timeout=180s
echo "Déployé : ${IMAGE}"
echo "UI : http://<IP-du-VPS>:30080"
