# Pipeline multi-agente su LangGraph

Il protocollo non è più un prompt da rispettare: è un grafo eseguito. I nodi
invocano gli agenti via CLI (nessun tetto di 10 minuti, nessuna richiesta di
permesso, nessuna working directory alla deriva), lo stato è tipizzato e
persistito su SQLite, e le escalation sono `interrupt()` di LangGraph.

## Installazione (Debian 12)

```bash
cd ~/Documenti/progetti/dnn-vtt/nexus-vtt
python3 -m venv .venv-pipeline
source .venv-pipeline/bin/activate
pip install langgraph langgraph-checkpoint-sqlite
# copia qui dentro: pipeline_graph/ e run.py
```

Verifica che i CLI siano raggiungibili (`devin`, `cursor-agent`, `gemini`,
`claude`): il preflight lo controlla e si ferma con un messaggio chiaro.

## I comandi

```bash
./run.py start  <ID> "<richiesta>" [--file F] [--auto] [--interview] [--ref P]
./run.py resume <ID> [--answer "..."]
./run.py redo   <ID> [--from plan|debate|visual]   # rifà una fase riusando gli artefatti
./run.py status <ID>
./run.py doctor <ID>                               # cos'è andato storto (fallimenti, degradi, agenti)
./run.py graph
```

`--auto` salta i checkpoint umani (approvazione del piano, intervista); le
escalation restano sempre. Senza `--auto` il grafo ti aspetta.

`redo` riposiziona il grafo a inizio fase riusando ciò che c'è: `--from debate`
riusa brief+piano e rifà il dibattito; `--from visual` riusa la UI costruita e
rifà solo render + cancello visivo (utile dopo aver sistemato un agente o gli
screenshot). `doctor` legge `events.jsonl` e ti dà in un colpo crash, agenti
`unhealthy`, step `blocked`/`degraded`, notifiche e liveness.

---

## Uso: i casi

> **Quando vale l'intervista.** Ogni task interattivo costa almeno un giro di
> Gemini più la tua attesa prima ancora che parta il piano. Si ripaga quando la
> richiesta è vaga o quando c'è un documento di riferimento da cui possono
> uscire eccezioni che non conoscevi. Per un task piccolo o già specificato
> bene, `--auto --file` la salta e vai dritto al piano.

### 1. Ho un'idea, non una specifica

Il caso normale. Parti da una frase e lascia che sia il grafo a interrogarti.

```bash
./run.py start 006 "rendere pubblicabile la classe Mystic"
```

Il nodo `intake_ask` (Gemini) legge la richiesta, il repo e i riferimenti, poi
scrive le domande in `docs/tasks/TASK-006-intake.md`: ognuna con l'evidenza che
l'ha provocata e una riga `**A:**` vuota. Rispondi nel tuo editor, poi:

```bash
./run.py resume 006 --answer ok
```

Le tue risposte possono generare un nuovo giro — il loop continua finché
l'intervistatore scrive `docs/tasks/TASK-006-brief.md`. Quel brief diventa il
contratto: è ciò che il proposer legge, non più la frase iniziale.

Vie d'uscita: `--answer skip` chiude l'intervista e pianifica con quello che c'è;
dopo `PIPELINE_MAX_INTAKE_ROUNDS` giri (default 4) il grafo escala da solo.

### 2. Il task dipende da un documento esterno

```bash
./run.py start 006 "rendere pubblicabile la classe Mystic" \
  --ref ~/Scaricati/UAMystic3.pdf --ref ~/note/psionics.md
```

I file finiscono in `docs/tasks/TASK-006-refs/`. Se è un PDF e `pdftotext` è
installato, ci viene affiancata l'estrazione testuale — dare un PDF grezzo a un
agente è una monetina, e il prompt gli dice di preferire il `.txt`.

Questo è il caso in cui l'intervista rende di più: l'intervistatore ha ordine di
cercare apposta i casi in cui la richiesta generalizza su qualcosa che il
riferimento contraddice.

### 3. La specifica ce l'ho già scritta

```bash
./run.py start 005 --file docs/tasks/TASK-005-mystic.md
```

`--file` è il seme, non la parola finale: l'intervista parte comunque e chiede
solo ciò che il documento lascia aperto. Se vuoi saltarla del tutto, aggiungi
`--auto` (vedi il caso 4) — il documento diventa il brief così com'è.

### 4. Run non presidiato

```bash
systemd-inhibit --what=sleep:idle --why="pipeline" tmux new -s pipeline
./run.py start 005 --file docs/tasks/TASK-005-mystic.md --auto
```

Nessuna domanda, nessun checkpoint: il grafo si ferma solo sulle escalation.
La combinazione più utile è però questa:

```bash
./run.py start 006 "frase secca" --auto --interview --ref ~/doc.pdf
```

Definisci il task con un umano davanti, poi tutto il resto gira da solo.

### 5. Riprendere

```bash
./run.py resume 005                     # dopo crash, suspend, terminale chiuso
./run.py resume 005 --answer ok         # sblocca un'escalation o un checkpoint
./run.py resume 005 --answer skip       # forza la chiusura del batch corrente
```

Lo stato è nel checkpointer SQLite: riparte dal nodo esatto, nessun LLM deve
indovinare dove eravamo. Se non c'è una domanda aperta, `resume` continua e
basta.

Le risposte che il grafo riconosce a un'escalation: `skip` / `close` / `force`
chiudono forzatamente il batch; qualunque altra risposta a un'escalation sui
test imposta `tests_waived` e va a `code_review` senza rilanciare
l'implementer; a un'intervista, `skip` / `done` / `stop` la chiudono.

### 6. Ho toccato il grafo e voglio sapere se regge

```bash
PIPELINE_DRY_RUN=1 ./run.py start 999 "prova" --auto
./run.py graph | less
```

Nessun agente viene invocato e **git non viene toccato** (niente branch nuovi,
niente commit WIP). Serve a verificare cablaggio, routing e scrittura dei file.
Ricordati di ripulire dopo: `docs/tasks/TASK-999-*`, `docs/prompts/999-*`,
`docs/metrics/journal-999.log`.

## Struttura

```
run.py                     CLI: start / resume / status / graph
pipeline_graph/
  config.py                ruoli -> agenti, path, limiti  <-- si tocca qui
  state.py                 stato tipizzato del grafo
  agents.py                subprocess + logging metriche + parser dei verdetti
  events.py                log ed eventi: il punto unico da cui passa tutto
  nodes.py                 un nodo per step del protocollo
  graph.py                 nodi, edge condizionali, checkpointer
  test_runner.py           gate dei test con baseline per batch
  prompts/*.md             i prompt di ogni ruolo
```

Artefatti su disco:

```
docs/tasks/TASK-<ID>-brief.md     il contratto del task (sempre scritto)
docs/tasks/TASK-<ID>-intake.md    domande e risposte dell'intervista
docs/tasks/TASK-<ID>-refs/        riferimenti passati con --ref
docs/plans/    docs/debates/    docs/reviews/
docs/final/    FINAL-<ID>.md, BATCHES-<ID>.json, PROGRESS-<ID>.md, REPORT-<ID>.md
docs/metrics/  pipeline.log, events.jsonl, journal-<ID>.log, runs.jsonl, raw/
```

## Dove si mette mano

- **Scambiare i ruoli** (A/B test implementer): `ROLES` in `config.py`. Due
  vincoli non negoziabili: `PROPOSER != PLAN_REVIEWER` e
  `IMPLEMENTER != CODE_REVIEWER`. `INTERVIEWER` è Gemini apposta — chi scrive il
  brief non deve poi pianificarci sopra, recensire quel piano o giudicarne la
  conformità.
- **Cambiare un prompt**: `pipeline_graph/prompts/<step>.md`. I placeholder
  `{task_id}`, `{batch_n}`, `{batch_scope}`, `{checklist_items}`, `{request}`,
  `{round}`, `{brief_path}`, `{intake_path}`, `{refs_path}`, `{refs_list}` sono
  sostituiti dai nodi.
- **Numero di round / cicli**: `MAX_DEBATE_ROUNDS`, `MAX_FIX_CYCLES`,
  `MAX_TEST_FIXES`, `MAX_INTAKE_ROUNDS` in `config.py`.
- **Gate test post-implement**: `pipeline_graph/test_runner.py` esegue
  `backend/npm test` e `frontend/npm test` (backend con `E2E_DATABASE_URL`
  da `config.py`). Chiavi FAIL prefissate `backend|` / `frontend|`. Prima del
  primo tentativo di batch con DB up cattura i FAIL come baseline (anche al
  retry se il DB era giù al tentativo 0); dopo l'implementer passa solo se non
  ci sono **nuovi** FAIL rispetto alla baseline, meno sottostringhe in
  `test_failure_allowlist` nel JSON del giudice (schema in `prompts/judge.md`).
- **Escalation sui test**: rispondere (non `skip`) imposta `tests_waived` e al
  resume va a `code_review` senza rilanciare l'implementer; `skip` chiude
  forzatamente il batch come prima.
- **Aggiungere uno step**: un nodo in `nodes.py` + un edge in `graph.py`.

## Il contratto con il giudice

Il nodo `judge` è l'unico che ragiona davvero (gira su `claude -p`). Deve
produrre due file: `FINAL-<ID>.md` (rulings, piano consolidato, conformance
checklist) e `BATCHES-<ID>.json`, che è ciò che guida il loop di
implementazione:

```json
[{"n": 1, "scope": "...", "checklist": [1,2,3],
  "test_failure_allowlist": ["creationMatrixManifest.test.ts > drift check"]}]
```
Ogni stringa in `test_failure_allowlist` matcha se è **sottostringa** della
chiave FAIL (es. `backend|src/...test.ts > suite > case`).

Se il JSON manca o è invalido il grafo escala invece di procedere alla cieca.
Il giudice può anche fermarsi da solo scrivendo `ESCALATE: <motivo>`.

## Il flusso, in ordine

```
init ──▶ intake_ask ⇄ intake_wait ──▶ plan
   ──▶ dibattito a DUE critici: debate_tech (tecnica) + debate_ux (designer),
       ⇄ debate_reply, fino a convergenza o cap
   ──▶ summary ──▶ judge ──▶ checkpoint_plan
   ──▶ per ogni batch: implement ──▶ code review ──▶ fix ──▶ verify ──▶ commit
   ──▶ [se UI] cancello visivo: ux_render ──▶ ux_visual_review ⇄ ux_visual_fix
   ──▶ final gate ──▶ wrap up
```

Il critico UX (designer) sta **dentro il dibattito**, in parallelo al critico
tecnico, non come coda post-giudice: plasma il piano dall'inizio. È autorevole
sulla UX, e l'unica uscita con un blocker aperto è un `TECH-LIMIT` verificato dal
critico tecnico sul codice.

Invariante: nessun artefatto è rivisto da chi l'ha prodotto. Qualunque nodo può
impostare `escalation` e finire su `escalate`, che ti chiede cosa fare; un crash
dentro un nodo diventa un'escalation con traceback, non la morte del processo.

## Il cancello visivo (gli occhi)

Ogni altro cancello legge **testo** (piani, diff) e non può vedere che una UI è
stretta, vuota, o che due modalità "diverse" renderizzano identiche. Per i task
con UI (`has_ui`, dichiarato dall'analista nel brief con `UI-SURFACE: yes`), dopo
i batch la pipeline **guarda i pixel**:

- **`ux_render`** tira su lo stack e2e, **semina le fixture** (Fighter + Wizard a
  id fissi, `scripts/e2e-seed-ux-fixtures.sh`), e lancia lo spec Playwright
  (`frontend/tests/e2e/ux-render.spec.ts`) che screenshotta Combat/Explore a
  1280px ed emette **fatti deterministici**: `overflow_x`, `page_scroll_y`,
  `mode_identical` (screenshot byte-uguali = le modalità non cambiano niente),
  `board_coverage` (spazio vuoto).
- **`ux_visual_review`** — VISUAL_REVIEWER (claude, che legge davvero i PNG) —
  critica il renderizzato contro i fatti + il manifesto. Verdetto + blocker.
- **`ux_visual_fix`** — VISUAL_FIXER (claude, che pure **vede** gli screenshot)
  corregge; il vedere è ciò che evita il whack-a-mole (fixare una modalità e
  romperne un'altra alla cieca). Poi ri-render.

Loop fino a `MAX_UX_RENDER_CYCLES` (default 3, env-overridabile). Se i blocker
**non calano per due cicli** (oscillazione), escala *prima* del cap: di solito
serve una decisione di design, non altri cicli. Deterministici **+** visione
insieme: i fatti prendono i sintomi misurabili (modalità identiche, scroll), la
vista prende ciò che nessuno strumento becca (un'etichetta troncata a `overflow_x
= false`). Disabilitabile con `PIPELINE_UX_RENDER_CMD` vuoto.

Prerequisito una-tantum: `cd frontend && npx playwright install chromium
chromium-headless-shell`.

## Cosa NON risolve

Se il processo muore (crash, suspend, terminale chiuso) il run muore con lui:
i checkpoint preservano lo stato, non l'esecuzione. Al ritorno serve un
`resume` manuale — che però è una riga, non una ricostruzione. Lancialo sotto
`systemd-inhibit ... tmux` come già facevi.

## Flusso di lavoro quotidiano

Terminale A (sul Debian, sotto tmux + systemd-inhibit):

```bash
systemd-inhibit --what=sleep:idle --why="pipeline" tmux new -s pipeline
source .venv-pipeline/bin/activate
./run.py start 005 --file docs/tasks/TASK-005-mystic.md --auto
```

Terminale B (monitor live):

```bash
tail -f docs/metrics/pipeline.log
```

Poi te ne vai.

Rientrando: `git diff main...feature/task-005` e review del branch.

## Log e notifiche

Ogni confine di step passa da `events.py`, che scrive su quattro canali insieme:
`events.jsonl` (macchina), `pipeline.log` (umano, `tail -f`),
`journal-<ID>.log` (per task) e ntfy (push). Un solo `emit()`, così uno step non
può essere loggato ma non notificato.

Cosa viene registrato: inizio e fine di ogni nodo con la durata, inizio e fine
di ogni chiamata agente con exit code e byte prodotti, escalation aperta,
**escalation risolta**, crash di nodo con traceback, degrado del DB, batch
chiuso, domande dell'intervista, fine run, e stallo del run.

```bash
./run.py status 005                    # nodo corrente, batch, pausa, log live
tail -f docs/metrics/pipeline.log      # tempo reale
tail -f docs/metrics/raw/005-impl-b1-glm-*.log   # output grezzo di un agente
python3 scripts/metrics-report.py 005  # tempi per step
```

`status` legge il **journal su file**, non quello checkpointato: lo stato del
grafo si aggiorna solo quando un nodo ritorna, quindi durante un implement da 40
minuti il journal checkpointato mostrerebbe ancora lo step precedente.

Il volume delle notifiche si regola con `PIPELINE_NOTIFY_LEVEL`:

| Valore | Cosa arriva sul telefono |
|---|---|
| `all` (default) | anche inizio e fine di ogni step |
| `milestones` | escalation, errori, domande, batch chiusi, fine run, stalli |
| `silent` | niente push; i log restano completi |

Il topic ntfy si prende da `NTFY_TOPIC` o dal file `.ntfy-topic` nella root.

## Variabili d'ambiente

| Variabile | Default | Effetto |
|---|---|---|
| `PIPELINE_DRY_RUN` | — | `1` = nessun agente, git intatto |
| `PIPELINE_NOTIFY_LEVEL` | `all` | `all` / `milestones` / `silent` |
| `PIPELINE_MAX_INTAKE_ROUNDS` | `4` | giri di intervista prima di escalare |
| `PIPELINE_AGENT_TIMEOUT` | nessuno | secondi; `0` o assente = nessun tetto |
| `PIPELINE_TEST_TIMEOUT` | vedi `config.py` | timeout del gate dei test |
| `PIPELINE_E2E_UP_TIMEOUT` | `660` | secondi per tirare su lo stack e2e |
| `PIPELINE_E2E_DATABASE_URL` | `localhost:5433` | DB usato da vitest lato host |
| `PIPELINE_MAX_UX_RENDER_CYCLES` | `3` | cicli render→review→fix del cancello visivo |
| `PIPELINE_UX_RENDER_CMD` | `npx playwright test …` | comando di render; **vuoto = disabilita il cancello visivo** |
| `PIPELINE_UX_RENDER_TIMEOUT` | `720` | secondi per il render (cold path fixture) |
| `PIPELINE_AGENT_TRANSIENT_RETRIES` | `1` | retry automatici su fallimenti agente transitori |
| `PIPELINE_REPO` | da `git rev-parse` | root del repo |

## Lo stack e2e

I nodi `implement` e `final_check` verificano che il Postgres e2e (`:5433`)
risponda e, se non risponde, provano a tirarlo su con `scripts/e2e-up.sh`.
Quello script ha due modi:

```bash
bash scripts/e2e-up.sh            # idempotente: up -d + wait-on, non distrugge
bash scripts/e2e-up.sh --fresh    # down -v + up --build: Postgres pulito e riseed
```

La pipeline usa sempre il primo. Se lo stack non parte **il run non si ferma**:
degrada, dice all'agente di saltare i test DB-gated e di dichiararlo, e il
report finale porta un avviso esplicito che quei test sono stati saltati, non
passati. Un problema di infrastruttura non blocca più il gate.

## Cosa è cambiato rispetto alla pipeline su Claude Code

Sparito perché non serve più: i divieti all'orchestratore (strutturalmente
impossibili da violare), le regole su come invocare gli agenti, il triage dello
Step -1 (ora sono comandi espliciti: start / resume / status), Remote Control.

Rimasto: il dibattito a max 2 round con fix verification, il verdetto con
conformance checklist, la review UX sul manifesto con correzione e delta review
tecnica, il loop a batch con code review e cicli di fix, il final gate, le
metriche.

Aggiunto dopo la migrazione:

- **L'intervista di intake**, che prima avveniva fuori dal grafo, in una chat
  con un modello. Il brief che ne esce è il contratto del task.
- **Un canale unico di log ed eventi** (`events.py`) al posto di tre `notify()`
  sparsi a mano, con il journal su file che si aggiorna *dentro* uno step lungo.
- **Il gate dei test con baseline per batch**, che distingue i FAIL nuovi da
  quelli che c'erano già.
- **Degrado invece di escalation** quando il DB e2e non risponde.
- **La regola dei conformance item**: devono essere verificabili leggendo il
  diff. Tutto ciò che richiede di *eseguire* qualcosa va al final gate e il code
  reviewer risponde `DEFERRED` — prima bruciava cicli di fix su cose che nessuno
  poteva verificare.

## LangGraph Studio (UI visuale, anche da telefono)

Studio è un debugger visuale che punta al server locale: vedi il grafo
disegnato, lo stato di ogni thread, la storia dei checkpoint, e puoi
**riprendere le escalation dalla UI** invece che da riga di comando.

```bash
pip install "langgraph-cli[inmem]"
langgraph dev            # stampa l'URL della Studio UI
langgraph dev --tunnel   # endpoint HTTPS pubblico -> apribile dal telefono
```

Il manifest è già incluso (`langgraph.json`, entrypoint
`pipeline_graph/app.py:graph`). Copia `.env.example` in `.env` e imposta
almeno `PIPELINE_REPO`.

**Come usarlo dal telefono**: lanci `langgraph dev --tunnel` sul Debian (dentro
tmux), salvi l'URL tra i preferiti del telefono, e da lì segui il grafo che
avanza e sblocchi gli interrupt. È il sostituto di Remote Control, con in più
la vista dello stato e la storia dei checkpoint.

### Due avvertenze importanti

1. **`langgraph dev` è un server in-memory pensato per sviluppo e test**: la
   persistenza è la sua, non il tuo SqliteSaver, e se il processo muore i thread
   se ne vanno. Per run lunghe e durature usa la CLI (`./run.py`, che usa
   SQLite su disco) oppure `langgraph up`, che tira su lo stack Docker con
   Postgres e Redis.
2. **La UI è ospitata su smith.langchain.com** e richiede un account LangSmith
   (free tier 5.000 tracce/mese). Il codice e l'esecuzione restano sulla tua
   macchina: il browser parla con il tuo server. Se non vuoi coinvolgere
   LangSmith, resta sulla CLI — non perdi nessuna funzionalità del protocollo,
   solo la vista grafica.

**Regola pratica**: `./run.py` per i run veri (durabile, headless, notifiche),
Studio quando modifichi il grafo o vuoi guardare/sbloccare da fuori.
