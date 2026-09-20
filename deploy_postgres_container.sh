#!/usr/bin/env bash
#
# postgres_container.sh
#
# Deploy (or remove) a PostgreSQL database running in a container, using
# whichever container runtime is available (Docker or Podman -- auto-detected).
#
# Usage:
#   ./postgres_container.sh deploy   [options]
#   ./postgres_container.sh undeploy [options]
#   ./postgres_container.sh status   [options]
#   ./postgres_container.sh logs     [options]
#
# Common options:
#   -n, --name NAME          Container name (default: postgres-dev)
#   -i, --image IMAGE        Image to run (default: postgres:16)
#   -p, --port PORT          Host port mapped to the container's 5432
#                             (default: 5432)
#   -u, --user USER          POSTGRES_USER (default: postgres)
#   -w, --password PASS      POSTGRES_PASSWORD (default: auto-generated on
#                             first deploy and printed once -- save it)
#   -d, --db NAME            POSTGRES_DB (default: same as --user)
#   -V, --volume NAME|PATH   Named volume or host path to persist data in
#                             (default: a named volume "<name>-data")
#   --no-persist             Don't mount any volume -- data is lost when the
#                             container is removed
#   --network NAME           Container network to attach to (optional)
#   --timeout SECONDS        How long to wait for Postgres to report ready
#                             on deploy (default: 30)
#
# undeploy-only options:
#   --purge                  Also remove the data volume, if any. This is
#                             determined by inspecting the container itself
#                             (not by re-passing --volume/--no-persist), and a
#                             host-path mount is always left alone -- this
#                             script will never delete a host directory for you
#   -f, --force               Don't prompt for confirmation before removing.
#                             Required if stdin isn't an interactive terminal
#                             (e.g. cron, CI) -- otherwise undeploy refuses to
#                             run rather than deleting without confirmation
#
# logs-only options:
#   -F, --follow              Follow the log output instead of a one-shot tail
#
#   -h, --help                Show this help
#
set -u -o pipefail

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
ACTION=""
NAME="postgres-dev"
IMAGE="postgres:16"
PORT="5432"
PG_USER="postgres"
PG_PASSWORD=""
PG_DB=""
VOLUME=""
NO_PERSIST=0
NETWORK=""
TIMEOUT=30
PURGE=0
FORCE=0
FOLLOW=0

RUNTIME=""

# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------
color() { local code="$1"; shift; if [[ -t 1 ]]; then printf "\033[%sm%s\033[0m" "$code" "$*"; else printf "%s" "$*"; fi; }
info()  { color "36" "$*"; }
ok()    { color "32" "$*"; }
warn()  { color "33" "WARN"; }
err()   { color "31" "ERROR"; }

log()  { printf "%s\n" "$*"; }
die()  { printf "%s: %s\n" "$(err)" "$*" >&2; exit 1; }

usage() { sed -n '2,40p' "$0" | sed 's/^# \{0,1\}//'; }

# ---------------------------------------------------------------------------
# Arg parsing: first positional token is the action, rest are flags
# ---------------------------------------------------------------------------
if [[ $# -eq 0 ]]; then
    usage
    exit 3
fi

case "$1" in
    deploy|undeploy|status|logs|help) ACTION="$1"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown action: $1" >&2; usage; exit 3 ;;
esac

while [[ $# -gt 0 ]]; do
    case "$1" in
        -n|--name)      NAME="$2"; shift 2;;
        -i|--image)     IMAGE="$2"; shift 2;;
        -p|--port)      PORT="$2"; shift 2;;
        -u|--user)      PG_USER="$2"; shift 2;;
        -w|--password)  PG_PASSWORD="$2"; shift 2;;
        -d|--db)        PG_DB="$2"; shift 2;;
        -V|--volume)    VOLUME="$2"; shift 2;;
        --no-persist)   NO_PERSIST=1; shift;;
        --network)      NETWORK="$2"; shift 2;;
        --timeout)      TIMEOUT="$2"; shift 2;;
        --purge)        PURGE=1; shift;;
        -f|--force)     FORCE=1; shift;;
        -F|--follow)    FOLLOW=1; shift;;
        -h|--help)      usage; exit 0;;
        *) echo "Unknown option: $1" >&2; usage; exit 3;;
    esac
done

[[ -z "$PG_DB" ]] && PG_DB="$PG_USER"
[[ -z "$VOLUME" && "$NO_PERSIST" -eq 0 ]] && VOLUME="${NAME}-data"

# ---------------------------------------------------------------------------
# Runtime detection: prefer docker, fall back to podman
# ---------------------------------------------------------------------------
detect_runtime() {
    if command -v docker >/dev/null 2>&1; then
        RUNTIME="docker"
    elif command -v podman >/dev/null 2>&1; then
        RUNTIME="podman"
    else
        die "neither docker nor podman found on PATH -- install one of them first"
    fi
}

# ---------------------------------------------------------------------------
# Container state helpers
# ---------------------------------------------------------------------------
container_exists() {
    "$RUNTIME" ps -a --filter "name=^${NAME}$" --format '{{.Names}}' 2>/dev/null | grep -qx "$NAME"
}

container_running() {
    "$RUNTIME" ps --filter "name=^${NAME}$" --format '{{.Names}}' 2>/dev/null | grep -qx "$NAME"
}

volume_looks_like_path() {
    local v="$1"
    [[ "$v" == /* || "$v" == ./* || "$v" == ../* ]]
}

# Looks up the actual persistent-data mount of a container as docker/podman
# recorded it -- NOT what --volume/--no-persist happen to default to on this
# invocation. This matters because undeploy runs as a separate process from
# deploy and has no memory of which flags were used to create the container;
# only the container's own metadata reliably reflects that.
#
# Prints "volume|<name>" or "bind|<host-path>" or nothing (no persistent
# mount was configured) on stdout.
get_persisted_mount() {
    "$RUNTIME" inspect "$NAME" --format \
        '{{ range .Mounts }}{{ if eq .Destination "/var/lib/postgresql/data" }}{{ .Type }}|{{ if eq .Type "volume" }}{{ .Name }}{{ else }}{{ .Source }}{{ end }}{{ end }}{{ end }}' \
        2>/dev/null
}

generate_password() {
    if command -v openssl >/dev/null 2>&1; then
        openssl rand -base64 18 | tr -dc 'A-Za-z0-9' | head -c 20
    else
        # Fallback with no external dependency
        tr -dc 'A-Za-z0-9' < /dev/urandom 2>/dev/null | head -c 20 || echo "pg$RANDOM$RANDOM"
    fi
}

wait_for_ready() {
    local elapsed=0
    printf "Waiting for Postgres to accept connections"
    while (( elapsed < TIMEOUT )); do
        if "$RUNTIME" exec "$NAME" pg_isready -U "$PG_USER" >/dev/null 2>&1; then
            printf " ready.\n"
            return 0
        fi
        printf "."
        sleep 1
        elapsed=$((elapsed + 1))
    done
    printf "\n"
    log "$(warn): Postgres did not report ready within ${TIMEOUT}s -- check '$RUNTIME logs $NAME'"
    return 1
}

print_connection_info() {
    log ""
    log "$(ok) Postgres is up:"
    log "  host     : localhost"
    log "  port     : $PORT"
    log "  user     : $PG_USER"
    log "  database : $PG_DB"
    if [[ -n "${1:-}" ]]; then
        log "  password : $1"
        log ""
        log "  (password was auto-generated -- save it now, it won't be shown again)"
    fi
    log ""
    log "  Connect with: psql -h localhost -p $PORT -U $PG_USER -d $PG_DB"
}

# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------
do_deploy() {
    detect_runtime

    if container_running; then
        log "$(ok) Container '$NAME' is already running -- nothing to do."
        print_connection_info
        return 0
    fi

    if container_exists; then
        log "Container '$NAME' exists but is stopped -- starting it."
        "$RUNTIME" start "$NAME" >/dev/null || die "failed to start existing container '$NAME'"
        wait_for_ready || true
        print_connection_info
        return 0
    fi

    local generated=0
    if [[ -z "$PG_PASSWORD" ]]; then
        PG_PASSWORD="$(generate_password)"
        generated=1
    fi

    local run_args=(-d --name "$NAME" \
        -e "POSTGRES_USER=$PG_USER" \
        -e "POSTGRES_PASSWORD=$PG_PASSWORD" \
        -e "POSTGRES_DB=$PG_DB" \
        -p "${PORT}:5432")

    if [[ "$NO_PERSIST" -eq 0 ]]; then
        run_args+=(-v "${VOLUME}:/var/lib/postgresql/data")
    fi
    if [[ -n "$NETWORK" ]]; then
        run_args+=(--network "$NETWORK")
    fi
    run_args+=("$IMAGE")

    log "Deploying '$NAME' from image '$IMAGE' on port $PORT ..."
    if ! "$RUNTIME" run "${run_args[@]}" >/dev/null; then
        die "failed to start container '$NAME' -- check the image name and that port $PORT is free"
    fi

    wait_for_ready || true

    if (( generated )); then
        print_connection_info "$PG_PASSWORD"
    else
        print_connection_info
    fi
}

do_undeploy() {
    detect_runtime

    if ! container_exists; then
        log "No container named '$NAME' found -- nothing to remove."
        return 0
    fi

    local was_running=0
    container_running && was_running=1

    # Capture the container's real persisted-mount info now, while the
    # container still exists -- we can't inspect it anymore after `rm`.
    local mount_info="" mount_type="" mount_ref=""
    if (( PURGE )); then
        mount_info="$(get_persisted_mount)"
        mount_type="${mount_info%%|*}"
        mount_ref="${mount_info#*|}"
    fi

    if [[ "$FORCE" -ne 1 ]]; then
        if [[ -t 0 ]]; then
            local prompt="Remove container '$NAME'"
            (( was_running )) && prompt+=" (currently running)"
            [[ "$PURGE" -eq 1 && -n "$mount_type" ]] && prompt+=" and its data ($mount_type '$mount_ref')"
            prompt+="? [y/N] "
            read -r -p "$prompt" reply
            case "$reply" in
                y|Y|yes|YES) ;;
                *) log "Aborted -- nothing removed."; return 0;;
            esac
        else
            die "confirmation required to remove '$NAME', but no interactive terminal was detected -- re-run with --force"
        fi
    fi

    if (( was_running )); then
        log "Stopping '$NAME' ..."
        "$RUNTIME" stop "$NAME" >/dev/null || die "failed to stop container '$NAME'"
    fi

    log "Removing container '$NAME' ..."
    "$RUNTIME" rm "$NAME" >/dev/null || die "failed to remove container '$NAME'"
    log "$(ok) Container '$NAME' removed."

    if (( PURGE )); then
        if [[ -z "$mount_type" ]]; then
            log "No persistent data mount was configured for this container -- nothing to purge."
        elif [[ "$mount_type" == "bind" ]]; then
            log "$(warn): data was stored at host path '$mount_ref', not a managed volume -- it will NOT be deleted."
            log "  Remove it yourself if you're sure: rm -rf '$mount_ref'"
        else
            log "Removing data volume '$mount_ref' ..."
            if "$RUNTIME" volume rm "$mount_ref" >/dev/null 2>&1; then
                log "$(ok) Volume '$mount_ref' removed."
            else
                log "$(warn): could not remove volume '$mount_ref' (it may not exist, or another container is using it)."
            fi
        fi
    fi
}

do_status() {
    detect_runtime

    if container_running; then
        log "$(ok) '$NAME' is running."
        "$RUNTIME" ps --filter "name=^${NAME}$" \
            --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}'
    elif container_exists; then
        log "$(warn): '$NAME' exists but is stopped."
        "$RUNTIME" ps -a --filter "name=^${NAME}$" \
            --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}'
    else
        log "'$NAME' is not deployed."
    fi
}

do_logs() {
    detect_runtime
    if ! container_exists; then
        die "no container named '$NAME' found"
    fi
    if (( FOLLOW )); then
        "$RUNTIME" logs -f "$NAME"
    else
        "$RUNTIME" logs --tail 50 "$NAME"
    fi
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
case "$ACTION" in
    deploy)   do_deploy;;
    undeploy) do_undeploy;;
    status)   do_status;;
    logs)     do_logs;;
    help)     usage;;
esac
