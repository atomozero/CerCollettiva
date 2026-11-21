# Audit del modulo `core/`

- **Base analizzata:** `main` al commit `3681b82` (novembre 2026)
- **File analizzati:** `core/models.py` (1363 righe), indice di `core/views/` e `core/services/`, `tests/test_models_core.py`, `.gitignore`
- **Riferimenti normativi:** D.Lgs. 8 novembre 2021 n. 199 (art. 31), DM MASE 7 dicembre 2023 n. 414 (Decreto CACER), Allegato A alla Delibera ARERA 727/2022/R/eel (TIAD), Regole operative GSE (Allegato 1 al DD MASE n. 22 del 23/02/2024 e successivi aggiornamenti)
- **Natura del documento:** analisi in sola lettura. Nessun file di codice è stato modificato. I riferimenti segnati "comma da verificare" vanno controllati sul testo vigente.

---

## A. Difetti tecnici bloccanti

1. **Le migrazioni non sono versionate.** Il `.gitignore` (righe 102-103) esclude `migrations/` e tiene solo `__init__.py`. Ogni installazione genera il proprio schema con `makemigrations` e non esiste uno storico condiviso. Così una modifica ai modelli non si può revisionare né portare in produzione in modo riproducibile.
2. **I test di `core` non corrispondono ai modelli.** `tests/test_models_core.py` usa campi che non esistono:
   - su `CERConfiguration`: `vat_number`, `admin`;
   - su `Plant`: `code`, `type`, `power_kw`, `pod`, `commissioning_date`, `cer`;
   - su `CERMembership`: `share_percentage` e il ruolo `VIEWER`.

   Questi test non possono passare, quindi `core` è di fatto senza test.
3. **Codice rotto o duplicato in `Plant`:**
   - `get_total_system_power` è definito due volte. La prima versione usa `DeviceMeasurement`, `Max` e `Sum` senza importarli.
   - `energy/views/plant_views.py:59` lo chiama con `user_id=`, un parametro che non esiste, quindi va in `TypeError`.
   - `get_energy_today`, `get_current_power` e `get_daily_energy_data` usano `self.mqtt_data`, ma nessun modello definisce quel `related_name`, quindi danno `AttributeError`.
   - C'è un `test_mqtt_connection` fuori dalle classi (righe 1229-1288), che è codice morto.
   - `Alert` dichiara due volte `status`, `severity`, `message` e `created_at`.
4. **Problemi di sicurezza e robustezza:**
   - `Plant.mqtt_password` è salvata in chiaro, mentre `CLAUDE.md` indica la cifratura a livello di campo (`django-encrypted-model-fields` è già tra le dipendenze).
   - `Plant.save()` chiama Nominatim in modo sincrono a ogni salvataggio senza coordinate, anche durante i test e gli import massivi.
   - `verify_gaudi` copia con `setattr` qualunque chiave ricevuta, senza una lista di campi ammessi.
5. **Numerazione delle tessere e del registro soci soggetta a race condition.** `MembershipCard.generate_card_number` usa `count()+1` e `MemberRegistry.register_member` usa `last+1`, senza lock. Con due iscrizioni contemporanee si ottengono numeri duplicati o un `IntegrityError`.

## B. Lacune di dominio CACER / TIAD

| Aspetto | Implementazione attuale | Cosa richiede la normativa |
|---|---|---|
| Soggetto CER e configurazioni | `CERConfiguration` ha un solo `primary_substation`, quindi 1 CER = 1 cabina primaria | Uno stesso soggetto giuridico CER può gestire più configurazioni, una per cabina primaria, ciascuna con il proprio codice di configurazione GSE e la propria rendicontazione |
| Cabina primaria | Campo di testo libero (`CharField(100)`) a livello CER | Il vincolo di perimetro vale per ogni punto di connessione, quindi la cabina primaria va collegata al singolo POD con il codice dell'area convenzionale pubblicata dal GSE (D.Lgs. 199/2021, art. 31; DM 414/2023, art. 2, comma da verificare) |
| POD | Esiste solo come `Plant.pod_code`, quindi solo per gli impianti | I POD dei consumatori, cioè i punti di prelievo, non vengono registrati. Senza di essi non si può ricostruire l'energia condivisa |
| Produttore terzo | `Plant.clean()` obbliga il proprietario dell'impianto a essere socio attivo | Il TIAD (Allegato A alla Del. ARERA 727/2022, art. 3.4 lett. f) consente che l'impianto nella disponibilità della CER sia di un produttore terzo non socio. Il vincolo attuale impedisce questo caso |
| Ruolo nella configurazione | `member_type` su `CERMembership` e di nuovo `plant_type` su `Plant` | Il ruolo produttore/consumatore dipende dal punto di connessione, non dalla persona: lo stesso soggetto può avere POD con ruoli diversi |
| Data di entrata in esercizio | Ci sono solo `installation_date` ed `expected_operation_date` (la data "presunta" di GAUDÌ) | Per l'accesso alla tariffa premio conta la data effettiva di entrata in esercizio e se viene prima o dopo la costituzione della CER (DM 414/2023, art. 3, comma da verificare) |
| Potenza | Cinque campi `FloatField` (`nominal_power`, `active_power`, `net_power`, `gross_power`, ...) senza dire quale sia di riferimento | Serve la potenza incentivabile per impianto o UP, che determina la fascia della tariffa premio (≤200 / ≤600 / >600 kW). Va anche controllato il limite di 1 MW per impianto (D.Lgs. 199/2021, art. 31, c. 2 lett. b) |
| Accumulo | Solo il booleano `has_storage` | Mancano potenza e capacità dell'accumulo e la sua posizione (lato produzione o lato post-produzione). Come incide sull'energia condivisa: riferimento normativo da verificare (TIAD e Regole operative GSE vigenti) |
| Incassi GSE | `GSEIncomeTracking` è per CER, con un solo importo e una sola tariffa | Le voci vanno separate per configurazione: tariffa premio (acconto e conguaglio), corrispettivo di valorizzazione ARERA, eventuale contributo PNRR, costi amministrativi GSE trattenuti. Inoltre `reference_month` (data) e `reference_year` sono ridondanti |

## C. Ripartizione economica

`CERDistributionConfiguration` assegna l'incentivo con percentuali fisse (45/30/20/3/2) uguali per tutta la CER. Ci sono due limiti:

- **Eccedenza della tariffa premio.** Il DM 414/2023, art. 3, c. 2, lett. g prevede che la parte di tariffa premio oltre la soglia percentuale di energia condivisa (Allegato 1 del decreto) vada solo ai consumatori diversi dalle imprese, o sia usata per finalità sociali con ricadute sul territorio. Per questo serve un calcolo e una contabilità separati, che il modello attuale non consente.
- **Criteri di ripartizione definiti dai regolamenti delle CER.** Molte CER ripartiscono la quota dei produttori in proporzione ai kWh immessi e conteggiati per la condivisione, oppure la trattengono in tutto o in parte per i fini statutari. Uno schema a percentuali fisse non rappresenta questi casi.

## D. Interventi proposti, in ordine

1. Versionare le migrazioni (togliere l'esclusione dal `.gitignore`) e fissare una baseline.
2. Riscrivere `tests/test_models_core.py` sui modelli reali prima di modificarli.
3. Correggere i difetti del punto A: metodi duplicati o rotti, campi duplicati di `Alert`, cifratura di `mqtt_password`, geocoding asincrono o differito, numerazioni dentro una transazione con lock.
4. Valutare una ristrutturazione del dominio: `SoggettoCER → Configurazione (codice GSE, cabina primaria) → PuntoPOD (prelievo/immissione, cabina primaria) → Impianto/UP (potenza incentivabile, data di esercizio, accumulo, produttore titolare o terzo)`.
5. Sostituire `CERDistributionConfiguration` con regole di ripartizione parametriche: criterio per i produttori (percentuale fissa o proporzionale ai kWh), trattenute dell'ente, gestione separata dell'eccedenza oltre soglia.

I punti 1-3 sono correzioni indipendenti e si possono proporre come PR separate. I punti 4-5 toccano l'architettura e vanno discussi prima di implementarli.
