#!/bin/bash

###########################################
#  CerCollettiva - Application Setup     #
#  Version: 1.0                          #
#  Author: Andrea Bernardi               #
#  Date: November 2024                   #
###########################################

# Previeni l'esecuzione diretta di questo script
if [ "${BASH_SOURCE[0]}" -ef "$0" ]; then
    echo "Questo script non dovrebbe essere eseguito direttamente"
    exit 1
fi

# Configurazione Django
setup_django() {
    log "INFO" "Configurazione Django..."
    update_progress "django" "in_progress"
    
    # Attiva virtualenv
    source "$VENV_PATH/bin/activate"
    
    # Crea file .env
    cat > "$APP_PATH/.env" << EOL
DEBUG=False
SECRET_KEY=$(python -c 'from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())')
ALLOWED_HOSTS=localhost,127.0.0.1,$(hostname -I | cut -d' ' -f1)
DATABASE_URL=sqlite:///db.sqlite3
MQTT_BROKER=${MQTT_BROKER:-"localhost"}
MQTT_PORT=${MQTT_PORT:-1883}
MQTT_USERNAME=${MQTT_USERNAME:-""}
MQTT_PASSWORD=${MQTT_PASSWORD:-""}
EOL

    log "SUCCESS" "File .env creato con successo"
    update_progress "django" "completed"
}

# Setup del database
setup_database() {
    log "INFO" "Configurazione database..."
    update_progress "database" "in_progress"
    
    source "$VENV_PATH/bin/activate"
    
    # Backup database se esiste
    if [ -f "$APP_PATH/db.sqlite3" ]; then
        log "INFO" "Backup database esistente..."
        cp "$APP_PATH/db.sqlite3" "$BACKUP_PATH/db_backup_$(date +%Y%m%d_%H%M%S).sqlite3"
    fi
    
    # Esegui migrazioni
    {
        python manage.py makemigrations --noinput
        if [ $? -ne 0 ]; then
            handle_error "Errore durante la creazione delle migrazioni"
        fi

        python manage.py migrate --noinput
        if [ $? -ne 0 ]; then
            handle_error "Errore durante l'applicazione delle migrazioni"
        fi
    } >> "$INSTALL_LOG" 2>&1
    
    log "SUCCESS" "Database configurato con successo"
    update_progress "database" "completed"
}

# Setup file statici
setup_static_files() {
    log "INFO" "Configurazione file statici..."
    update_progress "static" "in_progress"
    
    source "$VENV_PATH/bin/activate"
    
    # Crea directory per file statici
    mkdir -p "$APP_PATH/staticfiles"
    mkdir -p "$APP_PATH/media"
    
    # Colleziona file statici
    {
        python manage.py collectstatic --noinput
    } >> "$INSTALL_LOG" 2>&1
    
    if [ $? -ne 0 ]; then
        handle_error "Errore durante la raccolta dei file statici"
    fi
    
    # Imposta permessi
    sudo chown -R www-data:www-data "$APP_PATH/staticfiles"
    sudo chown -R www-data:www-data "$APP_PATH/media"
    sudo chmod -R 755 "$APP_PATH/staticfiles"
    sudo chmod -R 755 "$APP_PATH/media"
    
    log "SUCCESS" "File statici configurati con successo"
    update_progress "static" "completed"
}

# Setup MQTT
setup_mqtt() {
    log "INFO" "Configurazione MQTT..."
    update_progress "mqtt" "in_progress"
    
    # Verifica se usare broker locale o esterno
    if [[ -z "$MQTT_BROKER" || "$MQTT_BROKER" == "localhost" ]]; then
        log "INFO" "Configurazione broker MQTT locale..."
        
        # Installa Mosquitto se non presente
        if ! command -v mosquitto &> /dev/null; then
            {
                sudo apt install -y mosquitto mosquitto-clients
            } >> "$INSTALL_LOG" 2>&1
        fi
        
        # Configura Mosquitto
        sudo tee /etc/mosquitto/conf.d/default.conf > /dev/null << EOL
listener 1883
allow_anonymous false
password_file /etc/mosquitto/passwd
EOL
        
        # Crea utente MQTT default se non specificato
        if [[ -z "$MQTT_USERNAME" ]]; then
            MQTT_USERNAME="cercollettiva"
            MQTT_PASSWORD=$(openssl rand -base64 12)
            
            # Aggiorna .env con le nuove credenziali
            sed -i "s/MQTT_USERNAME=.*/MQTT_USERNAME=$MQTT_USERNAME/" "$APP_PATH/.env"
            sed -i "s/MQTT_PASSWORD=.*/MQTT_PASSWORD=$MQTT_PASSWORD/" "$APP_PATH/.env"
            
            # Crea utente Mosquitto
            sudo mosquitto_passwd -c /etc/mosquitto/passwd "$MQTT_USERNAME" "$MQTT_PASSWORD"
        fi
        
        # Riavvia Mosquitto
        sudo systemctl restart mosquitto
        sudo systemctl enable mosquitto
    fi
    
    # Test connessione MQTT
    log "INFO" "Test connessione MQTT..."
    timeout 5 mosquitto_sub -h "$MQTT_BROKER" -p "$MQTT_PORT" -u "$MQTT_USERNAME" -P "$MQTT_PASSWORD" -t "test" &>/dev/null
    
    if [ $? -ne 0 ]; then
        log "WARNING" "Test connessione MQTT fallito. Verificare le credenziali e la connettività."
    else
        log "SUCCESS" "Connessione MQTT verificata con successo"
    fi
    
    update_progress "mqtt" "completed"
}

# Creazione directory logs
setup_logs() {
    log "INFO" "Configurazione sistema di logging..."
    
    # Crea directory logs
    mkdir -p "$LOGS_PATH"
    
    # Configura logrotate
    sudo tee /etc/logrotate.d/cercollettiva > /dev/null << EOL
$LOGS_PATH/*.log {
    daily
    missingok
    rotate 7
    compress
    delaycompress
    notifempty
    create 0640 www-data www-data
}
EOL
    
    log "SUCCESS" "Sistema di logging configurato"
}

# Funzione principale setup applicazione
setup_application() {
    log "INFO" "Avvio setup applicazione..."
    
    # Backup preventivo
    create_backup
    
    # Sequenza setup
    setup_django
    setup_database
    setup_static_files
    setup_mqtt
    setup_logs
    
    log "SUCCESS" "Setup applicazione completato"
}