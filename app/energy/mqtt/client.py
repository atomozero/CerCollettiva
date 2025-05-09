# energy/mqtt/client.py
import logging
import threading
from typing import Optional
import paho.mqtt.client as mqtt
from django.conf import settings
from django.utils import timezone
from .manager import DeviceManager
from ..models import MQTTBroker, MQTTAuditLog
from .core import get_mqtt_service, MQTTMessage
import time
import json
from queue import Queue, Empty as QueueEmpty
from collections import deque


logger = logging.getLogger('energy.mqtt')

class EnergyMQTTClient:
    """Client MQTT per la gestione dei dispositivi energetici"""
    
    def __init__(self):
        """Initialize MQTT client with thread-safe message handling"""
        # Core MQTT settings
        self._client = None
        self._host = None 
        self._port = None
        self._username = None
        self._password = None
        self._use_tls = False
        self._initialized = False
        
        # State management 
        self._is_connected = False
        self._service = get_mqtt_service()
        self._last_message_time = None
        self._last_values = {}
        self._message_count = 0
        self._retry_delay = 1
        self._max_retry_delay = 60
        self._sock = None  # Aggiungiamo questo attributo per evitare errori _sock
        
        # Flag di controllo thread
        self._running = False
        self._heartbeat_running = False
        self._monitor_running = False
        self._reconnect_running = False
        
        # Thread safety
        self._lock = threading.Lock()
        self._subscription_lock = threading.Lock()
        self._subscribed_topics = set()
        
        # Message processing
        self._message_queue = Queue(maxsize=10000) # Limita coda
        self._message_buffer = deque(maxlen=1000)
        self._device_manager = DeviceManager()
        
        # Importa thread manager
        from .thread_manager import thread_manager
        self._thread_manager = thread_manager
        
        # Start worker threads
        self._thread_manager.start_thread(
            name="mqtt_message_processor", 
            target=self._process_queue, 
            daemon=True
        )
        self._start_heartbeat()
        
    # Convenience method for backwards compatibility
    def connect(self, host=None, port=None, username=None, password=None, use_tls=False):
        """Compatibility method to configure and start in one call"""
        if host and port:
            self.configure(host, port, username, password, use_tls)
        return self.start()

    def configure(self, host: str, port: int, username: str = None, 
                password: str = None, use_tls: bool = False):
        """Configura il client con i parametri di connessione"""
        try:
            # Validazione dell'host
            import socket
            try:
                socket.gethostbyname(host)  # Verifica che l'host sia valido
            except socket.gaierror:
                logger.error(f"Host non valido o non raggiungibile: {host}")
                return False

            self._host = host
            self._port = port
            self._username = username
            self._password = password
            self._use_tls = use_tls
            self._setup_client()
            self._initialized = True
            logger.info(f"MQTT client configured with host: {host}, port: {port}")
            return True
        except Exception as e:
            logger.error(f"Error configuring MQTT client: {e}")
            return False

    def _setup_client(self) -> None:
        """Configura il client MQTT con timeout e retry"""
        try:
            if not self._host:
                raise ValueError("Host not configured. Call configure() first.")
            
            client_id = f"CerCollettiva-{timezone.now().timestamp()}"
            self._client = mqtt.Client(client_id=client_id, clean_session=False)
            
            # Imposta timeout più breve per la connessione
            self._client.connect_timeout = 30.0  # Aumentato a 30 secondi
            self._client.reconnect_delay_set(min_delay=1, max_delay=60)
            
            # Imposta debugging
            logger.info(f"Configurazione client MQTT - Host: {self._host}, Port: {self._port}")
            logger.info(f"Username: {self._username}, Password: {'*****' if self._password else 'None'}")
            
            # Callbacks 
            self._client.on_connect = self._on_connect
            self._client.on_disconnect = self._on_disconnect
            self._client.on_message = self._on_message
            
            # Auth
            if self._username:
                logger.info(f"Impostazione credenziali MQTT: {self._username}")
                self._client.username_pw_set(self._username, self._password)
                
            # TLS
            if self._use_tls:
                logger.info("Attivazione TLS")
                self._client.tls_set()
            else:
                logger.info("TLS disattivato")
            
            # LWT
            self._client.will_set(
                "CerCollettiva/status",
                "offline",
                qos=1,
                retain=True
            )
            
        except Exception as e:
            logger.error(f"MQTT client setup error: {e}")
            raise

    def _process_queue(self):
        """Thread dedicato al processing dei messaggi"""
        self._running = True
        while self._running:
            try:
                # Usa timeout per poter controllare flag _running
                try:
                    msg = self._message_queue.get(timeout=1.0)
                    # Aggiungi al buffer circolare
                    self._message_buffer.append(msg)
                    # Processa il messaggio
                    self._process_message(msg)
                    self._message_queue.task_done()
                except QueueEmpty:
                    # Normale timeout, continua
                    continue
                except Exception as e:
                    logger.error(f"Error getting message from queue: {e}", exc_info=True)
            except Exception as e:
                logger.error(f"Error processing message from queue: {e}", exc_info=True)

    def _process_message(self, msg):
        """Processa un singolo messaggio"""
        try:
            # Evita refresh troppo frequenti usando una cache
            cache_key = f"last_refresh_{msg.topic}"
            last_refresh = getattr(self, '_last_refresh_time', {})
            current_time = time.time()
            
            if cache_key in last_refresh and current_time - last_refresh[cache_key] < 60:
                # Skip refresh se è passato meno di 1 minuto
                return
                
            payload = json.loads(msg.payload.decode('utf-8'))
            self._device_manager.process_message(msg.topic, payload)
            
            # Aggiorna timestamp ultimo refresh
            self._last_refresh_time = getattr(self, '_last_refresh_time', {})
            self._last_refresh_time[cache_key] = current_time
            
        except json.JSONDecodeError as e:
            logger.error(f"Error decoding JSON from {msg.topic}: {e}")
        except Exception as e:
            logger.error(f"Error processing message: {e}")

    def start(self) -> None:
        """Avvia il client MQTT"""
        try:
            if not self._initialized:
                raise ValueError("Client not configured. Call configure() first.")
                    
            logger.info(f"Connessione al broker MQTT {self._host}:{self._port}")
            logger.info(f"Credenziali: Username={self._username}, Password={'*****' if self._password else 'None'}")
            
            try:
                logger.info("Tentativo di connessione in corso...")            
                try:
                    result = self._client.connect(
                        self._host,
                        self._port,
                        keepalive=60
                    )
                    logger.info(f"Connect result code: {result}")
                except Exception as connect_err:
                    logger.error(f"Errore specifico in connect(): {str(connect_err)}", exc_info=True)
                    raise
                
                logger.info("Connessione riuscita, avvio loop...")
                
                self._client.loop_start()
                logger.info("Loop avviato")
                
                # Invece di usare is_connected, usa un metodo di check interno
                connection_timeout = 20  # Aumentato a 20 secondi
                start_time = time.time()
                
                logger.info("Verifica stato connessione...")
                attempt = 0
                while not self._check_connection_status():  # Nuovo metodo
                    attempt += 1
                    logger.info(f"Tentativo {attempt}: attesa connessione...")
                    if time.time() - start_time > connection_timeout:
                        logger.error(f"Timeout dopo {connection_timeout} secondi")
                        raise TimeoutError("Connection timeout")
                    time.sleep(0.5)
                
                logger.info("Connessione verificata con successo!")
                return True

            except TimeoutError:
                logger.error("MQTT connection timed out")
                self._reconnect_with_backoff()
                return False
                    
            except Exception as e:
                logger.error(f"MQTT client start error: {str(e)}", exc_info=True)
                self._reconnect_with_backoff()
                return False

        except Exception as e:
            logger.error(f"Error starting MQTT client: {str(e)}", exc_info=True)
            return False

    # Aggiungi questo nuovo metodo interno
    def _check_connection_status(self) -> bool:
        """Metodo interno per verificare lo stato della connessione"""
        with self._lock:
            if self._client is None:
                return False
            try:
                return (hasattr(self._client, 'is_connected') and 
                        self._client.is_connected() and 
                        self._is_connected)
            except Exception as e:
                logger.error(f"Errore verifica connessione: {e}")
                return False


    def check_connection(self) -> bool:
        """Verifica lo stato di connessione effettivo"""
        if self._client is None:
            return False
        try:
            return (hasattr(self._client, 'is_connected') and 
                    self._client.is_connected() and 
                    self._is_connected)
        except Exception as e:
            return False

    def stop(self) -> None:
        """Ferma il client MQTT"""
        if self._client:
            try:
                self._client.publish(
                    "CerCollettiva/status",
                    "offline",
                    qos=1,
                    retain=True
                )
                self._client.loop_stop()
                self._client.disconnect()
                
            except Exception as e:
                logger.error(f"MQTT client stop error: {e}")

    def _on_connect(self, client, userdata, flags, rc):
        """Callback per la connessione"""
        if rc == 0:
            with self._lock:  # Thread safety
                self._is_connected = True
                #print("\n=============== MQTT ==================")
                print(f" | Connessione al broker stabilita!    |")
                logger.info(f"ON_CONNECT: Connessione stabilita con successo!")
                logger.info(f"Broker: {self._host}:{self._port}")
                logger.info(f"Client ID: {client._client_id.decode()}")
                logger.info(f"Flags: {flags}")
                
                if self._subscribed_topics:
                    print(" | Topic sottoscritti:                |")
                    for topic in self._subscribed_topics:
                        print(f" | - {topic}")
                    logger.info(f"Topic sottoscritti: {self._subscribed_topics}")
                else:
                    print(" | Nessun topic sottoscritto         |")
                    logger.info("Nessun topic sottoscritto")
                
                self._subscribe_topics()
                self._publish_status("online")
                #print("========================================\n")
        else:
            error_msgs = {
                1: "Versione protocollo non corretta", 
                2: "Identificativo client non valido",
                3: "Server non disponibile",
                4: "Username o password non validi",
                5: "Non autorizzato"
            }
            error_msg = error_msgs.get(rc, f'Errore sconosciuto ({rc})')
            print("\n============== ERRORE ==================")
            print(f" | Connessione MQTT fallita!            |")
            print(f" | Motivo: {error_msg}")
            print("========================================\n")
            logger.error(f"MQTT connection failed: {error_msg} (code {rc})")
            logger.error(f"Host: {self._host}, Port: {self._port}, Username: {self._username}")
            with self._lock:
                self._is_connected = False
        
    def _on_disconnect(self, client, userdata, rc):
        """Gestione disconnessione migliorata"""
        self._is_connected = False
        if rc != 0:
            logger.warning("Unexpected disconnection from broker")
            self._reconnect_with_backoff()

    def _reconnect_with_backoff(self):
        def reconnect_thread():
            # Flag che controlla l'esecuzione del ciclo
            self._reconnect_running = True
            while self._reconnect_running and not self.check_connection():
                try:
                    logger.info(f"Tentativo di riconnessione tra {self._retry_delay}s...")
                    time.sleep(self._retry_delay)
                    
                    if not self._reconnect_running:
                        logger.info("Riconnessione interrotta")
                        break
                    
                    if self._client.reconnect() == 0:  # verifica il risultato della riconnessione
                        logger.info("Riconnessione riuscita")
                        break
                    else:
                        raise Exception("Riconnessione fallita")
                        
                except Exception as e:
                    logger.error(f"Tentativo di riconnessione fallito: {e}")
                    self._retry_delay = min(self._retry_delay * 2, self._max_retry_delay)

        # Usa thread manager per tracciare i thread
        self._thread_manager.start_thread(
            name="mqtt_reconnect",
            target=reconnect_thread,
            daemon=True
        )
                
    def _on_message(self, client, userdata, msg):
        """Callback per i messaggi ricevuti"""
        try:
            data = json.loads(msg.payload.decode('utf-8'))
            
            if msg.topic.endswith('/em:0'):
                device_id = msg.topic.split('/')[2]
                current_power = data.get('total_act_power')
                
                last_value = self._last_values.get(device_id)
                
                if current_power is not None:
                    # Se riceviamo uno 0 e abbiamo un valore precedente valido
                    if current_power == 0 and last_value:
                        time_diff = (timezone.now() - last_value['timestamp']).seconds
                        # Se sono passati meno di 5 minuti, manteniamo il valore precedente
                        if time_diff < 300:  # 300 secondi = 5 minuti
                            current_power = last_value['power']
                            logger.info(f"  Ignorato valore zero, mantenuto precedente: {current_power:.1f}W")
                    
                    self._last_values[device_id] = {
                        'power': current_power,
                        'timestamp': timezone.now()
                    }
                    logger.info(f"  Potenza Totale [W]: {current_power:.1f}")
                    
                elif last_value and (timezone.now() - last_value['timestamp']).seconds < 120:
                    logger.info(f"  Potenza Totale [W]: {last_value['power']:.1f} (mantenuto)")
            
            elif msg.topic.endswith('/emdata:0'):
                logger.info(f"  Energia Attiva Totale [kWh]: {data.get('total_act', 'N/A'):.2f}")
                
            self._device_manager.process_message(msg.topic, msg.payload)
            
        except json.JSONDecodeError as e:
            logger.error(f"Error decoding JSON from {msg.topic}: {e}")
            logger.error(f"Raw payload: {msg.payload}")
        except Exception as e:
            logger.error(f"Error processing message on {msg.topic}: {str(e)}")    


    def _subscribe_topics(self) -> None:
        """Gestisce le sottoscrizioni ai topic MQTT"""
        try:
            # Prima annulla tutte le sottoscrizioni esistenti
            for topic in list(self._subscribed_topics):
                try:
                    self.unsubscribe(topic)
                except Exception as e:
                    logger.error(f"Error unsubscribing from {topic}: {e}")
            
            # Ottieni i topic dei dispositivi attivi
            topics = self._device_manager.get_subscription_topics()
            
            # Se non ci sono dispositivi attivi, non sottoscrivere a nessun topic
            if not topics:
                logger.info("No active devices found, no topics to subscribe")
                return

            # Sottoscrivi solo ai topic dei dispositivi registrati
            for topic in topics:
                try:
                    result, mid = self._client.subscribe(topic, qos=1)
                    if result == 0:
                        self._subscribed_topics.add(topic)
                        #logger.info(f"Successfully subscribed to {topic}")
                    else:
                        logger.error(f"Failed to subscribe to {topic}")
                except Exception as e:
                    logger.error(f"Subscribe error for {topic}: {e}")

        except Exception as e:
            logger.error(f"Error in _subscribe_topics: {e}")

    def subscribe(self, topic: str) -> None:
        """Sottoscrive ad un topic MQTT"""
        if self._client and self._is_connected:
            try:
                result, mid = self._client.subscribe(topic, qos=1)
                if result == 0:
                    self._subscribed_topics.add(topic)
                    #logger.info(f"Successfully subscribed to {topic}")
                else:
                    logger.error(f"Failed to subscribe to {topic}")
            except Exception as e:
                logger.error(f"Subscribe error for {topic}: {e}")

    def unsubscribe(self, topic: str) -> None:
        """Annulla la sottoscrizione da un topic MQTT"""
        if self._client and self._is_connected:
            try:
                result, mid = self._client.unsubscribe(topic)
                if result == 0:
                    self._subscribed_topics.discard(topic)
                    #logger.info(f"Successfully unsubscribed from {topic}")
                else:
                    logger.error(f"Failed to unsubscribe from {topic}")
            except Exception as e:
                logger.error(f"Unsubscribe error for {topic}: {e}")

    def refresh_subscriptions(self) -> None:
        """Aggiorna tutte le sottoscrizioni"""
        if not self._client or not self._is_connected:
            logger.warning("Cannot refresh subscriptions: client not connected")
            return
            
        try:
            #logger.info("Refreshing MQTT subscriptions...")
            
            # Annulla tutte le sottoscrizioni esistenti
            for topic in list(self._subscribed_topics):
                self.unsubscribe(topic)
            
            # Usa il metodo esistente per sottoscrivere ai nuovi topic
            self._subscribe_topics()
            
            #logger.info(f"Subscriptions refreshed. Active topics: {len(self._subscribed_topics)}")
            
        except Exception as e:
            logger.error(f"Error refreshing subscriptions: {e}")

    def _publish_status(self, status: str) -> None:
        """Pubblica lo stato del client"""
        try:
            message = json.dumps({
                "status": status,
                "timestamp": timezone.now().isoformat(),
                "topics": len(self._subscribed_topics)
            })
            
            self._client.publish(
                "CerCollettiva/status",
                message,  # Ora il payload è una stringa JSON
                qos=1,
                retain=True
            )
            
        except Exception as e:
            logger.error(f"Status publish error: {e}")

    def cleanup(self):
        """Pulizia risorse alla chiusura"""
        try:
            if self._client:
                self._publish_status("offline")
                self._client.loop_stop()
                self._client.disconnect()
                
            # Arresta i thread
            if hasattr(self, '_processing_thread'):
                self._processing_thread.join(timeout=1.0)
                
        except Exception as e:
            logger.error(f"Error during cleanup: {e}")
        finally:
            self._initialized = False
            self._is_connected = False

    @property
    def is_connected(self):
        """Verifica lo stato di connessione effettivo"""
        with self._lock:
            # Verifica più sicura per evitare errori
            if self._client is None:
                return False
            try:
                return (hasattr(self._client, 'is_connected') and 
                        self._client.is_connected() and 
                        self._is_connected)
            except Exception as e:
                return False

    def refresh_configurations(self) -> None:
        """Aggiorna le configurazioni e rinnova le sottoscrizioni"""
        with self._lock:
            # Salva i valori correnti prima del refresh
            current_values = self._last_values.copy()
            
            # Aggiorna le configurazioni
            self._device_manager.refresh_configurations()
            
        if self._is_connected:
            # Rinnova le sottoscrizioni mantenendo i valori
            self._subscribe_topics()
            
            # Ripristina i valori precedenti
            self._last_values.update(current_values)
            
            # Log per debug
            logger.info("Configurazioni aggiornate mantenendo i valori esistenti")

    def force_refresh(self):
        """Forza il refresh delle configurazioni e delle sottoscrizioni"""
        logger.info("Forcing refresh of MQTT configurations")
        
        # Salva i valori correnti
        current_values = self._last_values.copy()
        
        self._device_manager.refresh_configurations()
        if self._is_connected:
            # Annulla le vecchie sottoscrizioni
            for topic in self._subscribed_topics:
                self._client.unsubscribe(topic)
            self._subscribed_topics.clear()
            
            # Sottoscrivi ai nuovi topic
            self._subscribe_topics()
            
            # Ripristina i valori precedenti
            self._last_values.update(current_values)

    def _start_heartbeat(self):
        """Avvia un thread di heartbeat in background usando ThreadManager"""
        from .thread_manager import thread_manager
        
        def heartbeat():
            self._heartbeat_running = True
            while self._heartbeat_running and self._is_connected:
                logger.info(f"""
                    === MQTT Client Heartbeat ===
                    Stato: {'Connesso' if self._is_connected else 'Disconnesso'}
                    Messaggi ricevuti: {self._message_count}
                    Ultimo messaggio: {self._last_message_time or 'Mai'}
                    Topics sottoscritti: {len(self._subscribed_topics)}
                    Host: {self._host}:{self._port}
                """)
                
                # Sleep con check periodico per terminazione
                for _ in range(60):  # 60 secondi
                    if not self._heartbeat_running or not self._is_connected:
                        break
                    time.sleep(1)
                    
        # Utilizza il thread manager per creare e tracciare il thread
        thread_manager.start_thread(
            name="mqtt_heartbeat", 
            target=heartbeat, 
            daemon=True
        )
        
    def _start_connection_monitor(self):
        """Monitora lo stato della connessione usando ThreadManager"""
        from .thread_manager import thread_manager
        
        def monitor():
            self._monitor_running = True
            while self._monitor_running:
                try:
                    with self._lock:
                        if self._client and self._client.is_connected() != self._is_connected:
                            logger.warning(f"Stato connessione discrepante: client={self._client.is_connected()}, internal={self._is_connected}")
                    
                    # Sleep con check periodico per terminazione
                    for _ in range(5):  # 5 secondi
                        if not self._monitor_running:
                            break
                        time.sleep(1)
                except Exception as e:
                    logger.error(f"Error in connection monitor: {e}")
                    time.sleep(5)
        
        # Utilizza il thread manager per creare e tracciare il thread
        thread_manager.start_thread(
            name="mqtt_connection_monitor", 
            target=monitor, 
            daemon=True
        )

def init_mqtt_connection():
    """
    Funzione di inizializzazione MQTT per l'avvio dell'applicazione.
    Gestisce il setup iniziale e la connessione del client MQTT.
    """
    try:
        time.sleep(5)  # Delay iniziale per permettere al DB di essere pronto
        #print("\n=========== MQTT Startup ===========")
        print(" Inizializzazione connessione MQTT")
        #print("===================================")

        # Ottieni l'istanza singleton del client
        client = get_mqtt_client()
        
        if client.is_connected:
            print(" | ✓ Client già connesso!           |")
            #print("===================================\n")
            return

        # Cerca una configurazione broker attiva
        try:
            from ..models import MQTTBroker
            mqtt_broker = MQTTBroker.objects.filter(is_active=True).first()
            
            if mqtt_broker:
                print(f" | Broker trovato: {mqtt_broker.host}   |")
                print(f" | Porta: {mqtt_broker.port}            |")
                
                # Test di connessione diretto con paho-mqtt
                try:
                    import paho.mqtt.client as mqtt_test
                    test_client = mqtt_test.Client("test_connection")
                    logger.info(f"Test di connessione MQTT con {mqtt_broker.host}:{mqtt_broker.port}")
                    if mqtt_broker.username:
                        test_client.username_pw_set(mqtt_broker.username, mqtt_broker.password)
                        logger.info(f"Impostate credenziali: {mqtt_broker.username}")
                    
                    logger.info("Tentativo di connessione test...")
                    result = test_client.connect(mqtt_broker.host, mqtt_broker.port, 5)
                    logger.info(f"Test connessione risultato: {result}")
                    test_client.disconnect()
                    logger.info("Test di connessione riuscito!")
                except Exception as test_error:
                    logger.error(f"Test di connessione fallito: {str(test_error)}")
                
                # Configura e avvia il client
                logger.info(f"Configurazione client principale con {mqtt_broker.host}:{mqtt_broker.port}")
                logger.info(f"Username: {mqtt_broker.username}, Password: {'****' if mqtt_broker.password else 'None'}")
                client.configure(
                    host=mqtt_broker.host,
                    port=mqtt_broker.port,
                    username=mqtt_broker.username,
                    password=mqtt_broker.password,
                    use_tls=mqtt_broker.use_tls
                )
                
                print(" | Avvio connessione...            |")
                success = client.start()
                logger.info(f"Risultato avvio client: {'Successo' if success else 'Fallito'}")
                
                # Verifica connessione
                time.sleep(2)
                if client.is_connected:
                    print(" | ✓ Connessione stabilita!         |")
                    logger.info("Connessione stabilita con successo!")
                else:
                    print(" | ✗ Connessione fallita!           |")
                    logger.error("Connessione MQTT fallita dopo l'avvio")
                #print("===================================\n")
            else:
                print(" | ✗ Nessun broker MQTT attivo      |")
                logger.warning("Nessun broker MQTT attivo trovato nel database")
                #print("===================================\n")
                
        except Exception as db_error:
            print(f" | ✗ Errore DB: {str(db_error)}        |")
            #print("===================================\n")
            logger.error(f"Database error during MQTT init: {db_error}")
            
    except Exception as e:
        print(f" | ✗ Errore inizializzazione: {str(e)} |")
        #print("===================================\n")
        logger.error(f"Error during MQTT initialization: {e}")
        raise

# Singleton instance
_mqtt_client = None

def get_mqtt_client() -> EnergyMQTTClient:
    global _mqtt_client
    if _mqtt_client is None:
        with threading.Lock():
            if _mqtt_client is None:
                _mqtt_client = EnergyMQTTClient()
    return _mqtt_client