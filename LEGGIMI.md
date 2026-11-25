# Monitoraggio Serchio: come attivarlo

Cosa fa: ogni pochi minuti apre la pagina del CFR con un browser, legge il livello di Vecchiano, ti manda una notifica sull'iPhone e aggiorna una dashboard. Avvisa anche quando supera la prima soglia (7,50 m) e la seconda (9,00 m).

## 1. Notifiche sull'iPhone
1. Installa l'app gratuita **ntfy** dall'App Store.
2. Tocca "+" e iscriviti a un argomento con un nome lungo e difficile da indovinare, per esempio `serchio-francesco-k7x2q9`. Chiunque conosca il nome può leggere le notifiche, quindi non usare un nome semplice.
3. Consenti le notifiche quando l'app lo chiede.

## 2. Repository su GitHub
1. Crea un repository **pubblico** (serve per la dashboard gratuita su GitHub Pages).
2. Carica `monitor.py`, `stations.json` e `index.html` (Add file, Upload files).
3. Add file, Create new file. Come nome scrivi esattamente `.github/workflows/serchio.yml` e incolla dentro il contenuto di `serchio.yml`. Poi Commit.

## 3. Impostazioni del repository
1. Settings, Secrets and variables, Actions, scheda Secrets: New repository secret. Nome `NTFY_TOPIC`, valore il nome dell'argomento scelto al punto 1.
2. Settings, Pages: Source "Deploy from a branch", branch `main`, cartella `/ (root)`. Dopo un minuto compare l'indirizzo della dashboard.
3. Scheda Variables: New repository variable. Nome `DASHBOARD_URL`, valore l'indirizzo della dashboard. Toccando una notifica si aprirà la dashboard.

## 4. Avvio
Scheda Actions, "Monitoraggio Serchio", Run workflow. Entro un paio di minuti deve arrivare la notifica "monitoraggio attivo" con i livelli letti. Controlla che siano metri e non portate.

## 5. Altri idrometri
Apri ogni stazione sul sito del CFR: nell'indirizzo c'è `id=TOS0200XXXX`. Aggiungi una riga in `stations.json`:

    { "id": "TOS0200XXXX", "name": "Nome stazione" }

## Quando arriva una notifica
Gli avvisi di andamento partono quando il livello arriva a 0,5 m sotto la prima soglia (a Vecchiano 7,00 m). Sotto quel livello i dati vengono solo registrati.
- Livello in salita: una notifica subito, poi una ogni 15 minuti finché continua a salire.
- Superamento della prima o della seconda soglia: notifica urgente subito.
- Prima lettura in discesa: notifica subito. Se continua a scendere, silenzio.
- Se risale: notifica subito e poi di nuovo una ogni 15 minuti.
- Salita che si ferma (3 letture di fila senza salita): una notifica "livello stabile".
- Se il CFR non risponde o i dati restano fermi più di un'ora: avviso con ⚠️. Il silenzio senza avvisi vuol dire che i dati arrivano regolarmente.

## Storico dei dati
- Le ultime 2 settimane restano complete, una lettura ogni pochi minuti (`data/history.json`).
- Quello che è più vecchio passa in `data/archive.json`, una volta al giorno circa, in forma ridotta: restano solo le letture in cui il livello cambia (salite, discese, picchi) e si tolgono quelle ferme. Ogni lettura tolta sta a meno di 2 cm dalla linea del grafico, quindi il grafico non cambia. In archivio resta comunque almeno un punto ogni 6 ore, così un buco nei dati si distingue da un livello costante.
- Sulla dashboard il pulsante **Periodo** arriva fin dove ci sono dati. Per i periodi lunghi la tabella ha una riga all'ora (poi ogni 6 ore o al giorno), con il livello più alto di quella fascia. Il grafico mostra un'interruzione dove mancano i dati.
- La cartella `data` la crea e la aggiorna lo script: non va caricata a mano e non va messa negli zip con cui aggiorni il progetto, altrimenti sovrascrivi lo storico.

## Limiti
- Il controllo gira ogni 5 minuti in una corsa che resta accesa e si rilancia da sola. Se GitHub la ferma, la pianificazione di sicurezza la riavvia entro mezz'ora circa.
- Non è un sistema di allerta ufficiale. In caso di pericolo seguire Comune e Protezione Civile, emergenze 112.
