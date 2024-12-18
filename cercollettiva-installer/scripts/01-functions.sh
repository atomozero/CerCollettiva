#!/bin/bash

###########################################
#  CerCollettiva - Functions File        #
#  Version: 1.0                          #
#  Author: Andrea Bernardi               #
#  Date: November 2024                   #
###########################################

# Previeni l'esecuzione diretta di questo script
if [ "${BASH_SOURCE[0]}" -ef "$0" ]; then
    echo "Questo script non dovrebbe essere eseguito direttamente"
    exit 1
fi

# Banner ASCII
show_banner() {
    clear
    echo -e "${CYAN}"
    cat << "EOF"
 =====================================================
   ______          ______    _ _     _   _   _           
  / ____/__  _____/ ____/___| | |___| |_| |_(_)_   ____ 
 / /   / _ \/ ___/ /   / __ \ | / _ \ __| __| \ \ / / _|
/ /___/  __/ /  / /___/ /_/ / |/  __/ |_| |_| |\ V / (_)
\____/\___/_/   \____/\____/|_|\___|\__|\__|_| \_/ \___/
                                                      
        Energy COMMUNITY Management Platform
 =====================================================
         [ Developed by Andrea Bernardi ]
         [ Version: 1.0 - November 2024  ]
 =====================================================
EOF
    echo -e "${NC}"
}

# Sistema di logging
log() {
    local level=$1
    local message=$2
    local timestamp=$(date '+%Y-%m-%d %H:%M:%S')
    
    # Crea directory per log se non esiste
    mkdir -p "$(dirname "$INSTALL_LOG")"
    
    echo "[$timestamp] [$level] $message" >> "$INSTALL_LOG"

    # Output colorato su console
    case $level in
        "INFO")    echo -e "${BLUE}[INFO]${NC} $message";;
        "SUCCESS") echo -e "${GREEN}[✔]${NC} $message";;
        "ERROR")   echo -e "${RED}[✘]${NC} $message";;
        "WARNING") echo -e "${YELLOW}[!]${NC} $message";;
    esac
}

# Gestione errori
handle_error() {
    local error_msg=$1
    local error_code=${2:-1}
    
    log "ERROR" "$error_msg"
    
    echo -e "\n${RED}╔════ ERRORE CRITICO ════╗${NC}"
    echo -e "${RED}║${NC} $error_msg"
    echo -e "${RED}╚════════════════════════╝${NC}"
    echo -e "${YELLOW}Log completo disponibile in: $INSTALL_LOG${NC}"
    
    # Mostra ultimi 5 errori dal log
    if [ -f "$INSTALL_LOG" ]; then
        echo -e "\nUltimi errori dal log:"
        grep "\[ERROR\]" "$INSTALL_LOG" | tail -n 5
    fi
    
    # Cleanup in caso di errore
    cleanup_on_error
    
    exit $error_code
}

# Sistema di progresso
show_progress() {
    clear
    show_banner
    
    # Informazioni Sistema essenziali
    echo -e "\n${BLUE}╔════════════════ Informazioni Sistema ════════════════╗${NC}"
    # CPU e Temperatura
    local cpu_usage=$(top -bn1 | grep "Cpu(s)" | awk '{print $2}')
    local cpu_temp=$(vcgencmd measure_temp 2>/dev/null | cut -d= -f2 | cut -d. -f1)
    echo -e "${BLUE}║${NC} CPU: ${GREEN}${cpu_usage}%${NC} (${GREEN}${cpu_temp}°C${NC})"
    
    # Memoria
    local mem_percent=$(free | grep Mem | awk '{print int($3/$2 * 100)}')
    echo -e "${BLUE}║${NC} RAM: ${GREEN}${mem_percent}%${NC} utilizzata"
    
    # Storage
    local disk_percent=$(df -h / | awk 'NR==2 {print $5}')
    echo -e "${BLUE}║${NC} Disco: ${GREEN}${disk_percent}${NC} utilizzato"
    
    # IP
    local ip_address=$(hostname -I | cut -d' ' -f1)
    echo -e "${BLUE}║${NC} IP: ${GREEN}${ip_address}${NC}"
    echo -e "${BLUE}╚══════════════════════════════════════════════════╝${NC}"

    # Stato Installazione
    local percentage=$((CURRENT_STEP * 100 / TOTAL_STEPS))
    local bar_width=50
    local completed=$((percentage * bar_width / 100))
    
    echo -e "\n${BLUE}╔════════════════ Stato Installazione ════════════════╗${NC}"
    echo -e "${BLUE}║${NC} Progresso: ${GREEN}$percentage%${NC} completato"
    echo -e "${BLUE}║${NC} Task attuale: ${YELLOW}${TASKS[$CURRENT_TASK]}${NC}"
    
    # Barra progresso
    echo -ne "${BLUE}║${NC} ["
    for ((i=0; i<bar_width; i++)); do
        if ((i < completed)); then
            echo -ne "${GREEN}█${NC}"
        else
            echo -ne "░"
        fi
    done
    echo -e "]"
    
    # Task completati
    if [ ${#COMPLETED_TASKS[@]} -gt 0 ]; then
        echo -e "${BLUE}║${NC}"
        echo -e "${BLUE}║${NC} ${GREEN}Task Completati:${NC}"
        for task in "${COMPLETED_TASKS[@]}"; do
            echo -e "${BLUE}║${NC} ${GREEN}✓${NC} $task"
        done
    fi
    
    echo -e "${BLUE}╚══════════════════════════════════════════════════╝${NC}\n"
}

# Aggiorna progresso
update_progress() {
    local task_key=$1
    local status=$2
    CURRENT_TASK=$task_key
    
    if [[ "$status" == "completed" ]]; then
        CURRENT_STEP=$((CURRENT_STEP + 1))
        COMPLETED_TASKS+=("${TASKS[$task_key]}")
    fi
    
    show_progress
}

# Verifica connessione internet
check_internet() {
    log "INFO" "Verifica connessione internet..."
    if ! ping -c 1 8.8.8.8 &> /dev/null; then
        handle_error "Connessione Internet non disponibile"
    fi
    log "SUCCESS" "Connessione internet verificata"
}

# Spinner per operazioni lunghe
spinner() {
    local pid=$1
    local delay=0.1
    local spinstr='|/-\'
    while [ "$(ps a | awk '{print $1}' | grep $pid)" ]; do
        local temp=${spinstr#?}
        printf " [%c]  " "$spinstr"
        local spinstr=$temp${spinstr%"$temp"}
        sleep $delay
        printf "\b\b\b\b\b\b"
    done
    printf "    \b\b\b\b"
}

# Backup di sicurezza
create_backup() {
    local backup_name="backup_$(date +%Y%m%d_%H%M%S)"
    log "INFO" "Creazione backup di sicurezza: $backup_name"
    
    if [ -d "$APP_PATH" ]; then
        mkdir -p "$BACKUP_PATH"
        tar -czf "$BACKUP_PATH/$backup_name.tar.gz" -C "$APP_PATH" . &> /dev/null
        if [ $? -eq 0 ]; then
            log "SUCCESS" "Backup creato con successo"
        else
            log "WARNING" "Errore durante la creazione del backup"
        fi
    fi
}

# Cleanup vecchi backup
cleanup_old_backups() {
    if [ -d "$BACKUP_PATH" ]; then
        find "$BACKUP_PATH" -name "backup_*.tar.gz" -mtime +$BACKUP_RETENTION_DAYS -delete
    fi
}

# Cleanup in caso di errore
cleanup_on_error() {
    log "INFO" "Pulizia dopo errore..."
    rm -f "$APP_PATH/.install_error"
    rm -f "$APP_PATH/.install_progress"
    rm -f "/tmp/cercollettiva_install.lock"
}

# Funzione di pulizia generale
cleanup() {
    log "INFO" "Pulizia file temporanei..."
    rm -f "$APP_PATH/.install_error"
    rm -f "$APP_PATH/.install_progress"
    rm -f "/tmp/cercollettiva_install.lock"
    cleanup_old_backups
}

# Trap per gestire interruzioni
trap 'handle_error "Installazione interrotta" 130' INT TERM
