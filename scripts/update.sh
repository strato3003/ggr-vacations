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
  # stdout du build / import ne doit jamais alimenter IMAGE (sinon InvalidImageName).
  as_root docker build -t "$img" "$ROOT"
  as_root docker save "$img" | as_root k3s ctr images import -
}

IMAGE=""
case "$MODE" in
  local)
    IMAGE="ggr-vacations:local"
    build_local
    ;;
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
      IMAGE="ggr-vacations:local"
      build_local
    fi
    ;;
  *)
    echo "usage: $0 [auto|local|pull]" >&2
    exit 1
    ;;
esac

ensure_cert_manager() {
  if kc get crd certificates.cert-manager.io >/dev/null 2>&1; then
    return 0
  fi
  echo "Installation cert-manager v1.13.2 (certificats Let's Encrypt)…"
  kc apply -f https://github.com/cert-manager/cert-manager/releases/download/v1.13.2/cert-manager.yaml
  kc -n cert-manager rollout status deploy/cert-manager-webhook --timeout=180s
  kc -n cert-manager rollout status deploy/cert-manager-cainjector --timeout=180s
  kc -n cert-manager rollout status deploy/cert-manager --timeout=180s
}

apply_clusterissuer() {
  local i
  for i in 1 2 3 4 5 6; do
    if kc apply -f "$ROOT/k8s/clusterissuer.yaml"; then
      return 0
    fi
    echo "ClusterIssuer : webhook cert-manager pas prêt, nouvel essai (${i}/6)…"
    sleep 5
  done
  return 1
}

ensure_cert_manager
kc apply -f "$ROOT/k8s/namespace.yaml"
apply_clusterissuer
kc apply -k "$ROOT/k8s"
kc -n "$KNS" set image "deploy/ggr-vacations" "web=${IMAGE}"

if [[ "$IMAGE" == ghcr.io/* ]]; then
  kc -n "$KNS" patch deploy ggr-vacations --type json \
    -p '[{"op":"replace","path":"/spec/template/spec/containers/0/imagePullPolicy","value":"Always"}]'
fi

kc -n "$KNS" rollout restart "deploy/ggr-vacations"
kc -n "$KNS" rollout status "deploy/ggr-vacations" --timeout=180s
echo "Déployé : ${IMAGE}"
echo "UI : https://ggr-vacations.k3s.lpb.ovh"
