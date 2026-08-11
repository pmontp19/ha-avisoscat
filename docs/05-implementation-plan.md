# Pla d'implementació — `ha-avisoscat`

Descomposició en tasques derivada de [`03-feature-spec.md`](03-feature-spec.md) i
[`04-architecture.md`](04-architecture.md). Cada tasca és S/M (1–5 fitxers), deixa el
sistema en verd i té criteris d'acceptació verificables.

## Graf de dependències

```
T1 Scaffold + CI
    ├── T2 Models (pur Python)
    │       ├── T3 Parser del payload públic        ⚠️ risc alt
    │       │       └── T5 Client SMP (dual)
    │       │               └── T7 Coordinator + events
    │       │                       ├── T8 Sensors de nivell
    │       │                       ├── T9 Sensors per meteor
    │       │                       ├── T10 Binary sensors
    │       │                       └── T13 Diagnosi + resiliència
    │       └── T4 Vigència (franges de 6 h)        ⚠️ risc alt
    └── T6 Comarques + point-in-polygon
            └── T11 Config flow
                    └── T12 Options flow + reauth
T14 Traduccions      ← (T8–T13)
T15 Blueprint        ← (T10, T7)
T16 README + HACS    ← (tot)
```

**Ordre de risc**: T3 (extracció del payload d'una pàgina HTML de tercers) i T4 (lògica de
vigència i horitzons) van al principi. Si l'extracció no és robusta o la projecció
"en vigor / anunciat" no quadra, ho volem saber abans d'escriure cap entitat.

Els plans de Protecció Civil **no hi són**: van a `ha-cecat`, una integració separada
([`02-existing-integrations.md`](02-existing-integrations.md) §8).

---

## Fase 1 — Fonament (sense runtime de HA)

### Task 1 — Scaffold del repositori i CI

Crear l'esquelet del §1 de `04-architecture.md`: `custom_components/avisoscat/` amb
`manifest.json` (§2), `const.py` (domini, URLs del §7 de `01-data-sources.md`, defaults),
`hacs.json`, `pyproject.toml` (ruff + pytest, `requires-python >=3.13`), `LICENSE` MIT,
`CONTRIBUTING.md` amb la convenció de commits, i els dos workflows.

**Acceptació**
- [ ] `manifest.json` vàlid: `domain: avisoscat`, `integration_type: service`,
      `iot_class: cloud_polling`, `requirements: []`, `config_flow: true`, **sense**
      `single_config_entry`
- [ ] Tots els endpoints del §7 de `01-data-sources.md` com a constants a `const.py`
- [ ] CI falla si `ruff` o `hassfest` fallen

**Verificació:** `ruff check .` net; `validate.yml` verd. **Mida:** M

### Task 2 — Models

`models.py`: enums `Meteor`, `TipusAvis`, `NivellPerill` (amb `from_perill()`), dataclasses
`Afectacio`/`Evolucio`/`Avis`/`Episodi`/`Preavis`/`SmpSnapshot`, i `parse_snapshot()`
tolerant. Cap import de HA.

**Acceptació**
- [ ] `NivellPerill.from_perill()` implementa el mapatge oficial verificat: `0→cap`,
      `1-2→moderat`, `3-4→alt`, `5-6→molt_alt`
- [ ] **Un test per cada trap de `01-data-sources.md` §6** (11 tests com a mínim):
      floats `2.0`, `afectacions: null`, `estat: "Ampliat"` no filtrat, dedup per
      `dataEmisio`, meteor desconegut → `None` + warning, variants del literal del tipus,
      `idMeteor: null`, franja `"18-00"`, `idComarca` desconegut
- [ ] `parse_snapshot()` no llança mai amb entrada malformada: retorna un snapshot buit

> El trap 12 de `01-data-sources.md` §6 es va documentar **després** de tancar aquesta
> tasca i per tant no forma part d'aquests criteris: es va tancar més tard a `models.py`
> (`Avis.afectacions_directes` i `Avis.totes_afectacions`).

**Verificació:** `pytest tests/test_models.py`. **Dependències:** T1. **Mida:** M

### Task 3 — Parser del payload públic ⚠️ risc alt

`parser.py`: `extract_smp_payload(html) -> tuple[list, list]`. Localitza
`Meteocat.avisosSMP(`, extreu els arrays de `avisos:` i `episodisPreavisos:` amb comptador
de claudàtors **conscient de les cadenes** i llegeix les claus **només al primer nivell**
de la crida, cosa que exclou estructuralment tant la clau `avisos` buida de l'objecte
`opcions` com la que porta cada episodi del payload. Entre les còpies de la crida es queda
amb la **més rica**, no amb la primera que contingui episodis: així el visor d'1 dia de la
portada no descarta en silenci els avisos de demà. Retorna JSON parsejat. Capturar la
fixture `smp_page_sample.html` (pàgina real, retallada però amb el payload intacte).

**Acceptació**
- [ ] Extreu correctament el payload de la fixture real del 2026-08-06
- [ ] Un `comentari` que conté `[`, `]` o `{` no trenca l'extracció (test dedicat)
- [ ] Pàgina sense episodis → `([], [])`, no excepció
- [ ] Pàgina sense `Meteocat.avisosSMP(` → `SmpParseError`

**Verificació:** `pytest tests/test_parser.py`; script manual documentat contra
`https://www.meteo.cat/observacions/radar` i `https://www.meteo.cat/`.
**Dependències:** T2. **Mida:** M

### Task 4 — Vigència i horitzons ⚠️ risc alt

`vigencia.py`: `periode_actual()`, `afectacions_vigents()` (en vigor ara),
`afectacions_anunciades()` (emeses, encara no vigents), `outlook()` (graella dia × franja
per als 3 dies) i el cas especial de temps violent (finestra de 2 h des de `dataEmisio`).
Tot en UTC.

És el mòdul que materialitza els dos horitzons del §1.1 de `03-feature-spec.md`.

**Acceptació**
- [ ] Un avís amb afectació només a `12-18` és **vigent** a les 13:00 UTC, **anunciat** a
      les 11:59, i cap de les dues coses a les 19:00
- [ ] Un avís amb `dataFi` a mitja franja deixa de ser vigent a `dataFi`, no al final de
      la franja
- [ ] La franja `18-00` cobreix de 18:00 a 23:59 UTC
- [ ] `outlook()` retorna les 4 franges de cadascun dels 3 dies, amb 0 on no hi ha afectació
- [ ] `hores_per_endavant` és correcte per a un avís emès avui per a demà passat
- [ ] Temps violent: vigent 2 h des de `dataEmisio`, ignorant franges, i **mai** anunciat
- [ ] Els tests fan servir el `FakeClock` de `conftest.py`, mai `freezegun` ni `sleep()`

**Verificació:** `pytest tests/test_vigencia.py`. **Dependències:** T2. **Mida:** M

### Task 5 — Client SMP dual

`smp.py`: `SmpSource` (Protocol), `PublicPageSource` (amb *fallback* a
`https://www.meteo.cat/`) i `ApiKeySource` (`x-api-key`, `/pronostic/v2/smp/episodis-oberts`,
`/…/preavisos`, `/quotes/v1/consum-actual`). Retries amb backoff per 5xx/timeout, cap retry
per 4xx, `403 → ConfigEntryAuthFailed`, `429 → UpdateFailed` sense retry. `payload_hash`
al snapshot.

**Acceptació**
- [ ] Amb `aioresponses`, `PublicPageSource` prova el *fallback* només quan la pàgina
      primària falla (error de descàrrega o `SmpParseError`); un resultat buit **no**
      l'activa
- [ ] `ApiKeySource` envia la capçalera `x-api-key` i mai la registra al log
- [ ] `429` no genera cap reintent (verificat comptant crides)
- [ ] Les dues fonts produeixen un `SmpSnapshot` idèntic a partir del mateix JSON

**Verificació:** `pytest tests/test_smp.py`. **Dependències:** T3. **Mida:** M

### Task 6 — Comarques i point-in-polygon

`comarques.py`: taula estàtica de 55 entrades (43 + 12 marítimes, §4.2 de
`01-data-sources.md`) amb la zona marítima adjacent; decodificació de TopoJSON i ray
casting purs per al config flow; `nom(id)` amb fallback `f"Comarca {id}"`.

**Acceptació**
- [ ] La taula conté Moianès (42) i Lluçanès (43)
- [ ] `nom(77)` retorna `"Comarca 77"` amb warning, no `KeyError`
- [ ] Point-in-polygon: Vic → Osona, Barcelona → Barcelonès, un punt a Aragó → `None`
- [ ] Cap dependència nova (`shapely` prohibit)

**Verificació:** `pytest tests/test_comarques.py`. **Dependències:** T1. **Mida:** M

### Checkpoint 1 — Fonament
- [ ] `pytest` + `ruff` + `hassfest` verds
- [ ] Extracció live de `meteo.cat` funciona i l'esquema coincideix amb
      `01-data-sources.md` (si no: aturar-se i actualitzar el document primer)

---

## Fase 2 — Slice vertical (avisos anunciats i en vigor)

### Task 7 — Coordinator i events

`coordinator.py` (`AvisoscatDataUpdateCoordinator`, §7–§8 de `04-architecture.md`):
`AvisoscatState` amb `en_vigor` / `anunciats` / `outlook`, els dos bucles d'emissió
(`_emit_announced` i `_emit_in_force`), conservació de l'estat en cas de fallada,
`always_update=False`. `__init__.py` amb `async_setup_entry`, `runtime_data` tipat i el
`async_track_time_change` d'1 minut que força el recàlcul **sense petició de xarxa**.
Interval adaptatiu 30/10 min segons si hi ha episodis oberts.

**Acceptació**
- [ ] Avís nou emès per a demà → `avisoscat_warning_announced` amb `hores_per_endavant`
      correcte, i **cap** `started`
- [ ] En arribar la franja → `avisoscat_warning_started` amb `anunciat_amb_hores`
- [ ] Grau 2→4 → `upgraded`; 4→2 → `downgraded`; desaparició → `cleared` amb `motiu`
- [ ] El mateix anunci en dos cicles seguits **no** es repeteix; una ampliació
      (`dataEmisio` nou) **sí**
- [ ] Arrencar amb avisos ja emesos des de fa dies **no** dispara cap `announced`
- [ ] `avisoscat_violent_weather` es dispara **un cop per `dataEmisio`**, no cada cicle
- [ ] Amb un fetch que falla, `state.en_vigor` es manté i `last_error` s'omple
- [ ] El canvi de franja a 12:00 UTC genera els events **sense cap crida HTTP** (test amb
      `FakeClock` i `aioresponses` sense mocks nous)
- [ ] L'interval passa de 30 a 10 min quan apareix el primer episodi obert

**Verificació:** `pytest tests/test_coordinator.py tests/test_events.py`.
**Dependències:** T4, T5. **Mida:** L (dividir si cal: coordinator / events)

### Task 8 — Sensors de nivell

`sensor.py`: `nivell_d_avis` (§3.1), `avisos_actius` (§3.2), `avis_anunciat` (§3.3), els
tres `grau_maxim_*` amb la seva graella (§3.4) i `preavis` (§3.6). `entity.py` amb la base
i el `DeviceInfo`.

**Acceptació**
- [ ] `nivell_d_avis` i `avis_anunciat` són `SensorDeviceClass.ENUM` amb
      `options=["cap","moderat","alt","molt_alt"]`
- [ ] Un avís emès per a demà deixa `nivell_d_avis` a `cap` i `avis_anunciat` a `alt` —
      **el test que evita l'error de disseny del §1.1**
- [ ] `avis_anunciat` exposa `comenca`, `hores_per_endavant` i `dia`
- [ ] `grau_maxim_dema.graella` té exactament les 4 franges
- [ ] `avisos_actius` té `state_class: MEASUREMENT`

**Verificació:** `pytest tests/test_sensor.py`. **Dependències:** T7. **Mida:** M

### Task 9 — Sensors per meteor

Els 10 sensors del §3.4, creats només per als meteors seleccionats a les opcions, amb
`graus_per_periode`.

**Acceptació**
- [ ] Amb `meteors: ["vent"]` només es crea `avis_vent`
- [ ] `graus_per_periode` retorna les 4 franges del dia en curs
- [ ] Un meteor desconegut a la font no crea cap entitat i deixa un warning

**Verificació:** `pytest tests/test_sensor_meteors.py`. **Dependències:** T8. **Mida:** M

### Task 10 — Binary sensors

`binary_sensor.py`: `avis_actiu`, `avis_greu`, `avis_greu_anunciat`, `temps_violent`
(§3.8–§3.11). Tots amb `device_class: SAFETY`.

**Acceptació**
- [ ] `avis_greu` i `avis_greu_anunciat` respecten `severe_threshold` i canvien en
      reconfigurar-lo
- [ ] Un avís greu per a demà encén `avis_greu_anunciat` i **no** `avis_greu`
- [ ] `temps_violent` s'apaga sol en passar les 2 h, sense fetch nou

**Verificació:** `pytest tests/test_binary_sensor.py`. **Dependències:** T7. **Mida:** S

### Task 11 — Config flow (passa 1 + passa 2)

`config_flow.py`: `LocationSelector` sense radi preomplert amb `zone.home`, resolució a
comarca amb *fallback* al desplegable, `unique_id` per comarca amb
`_abort_if_unique_id_configured()`, i la passa d'opcions (§2 de `03-feature-spec.md`) amb
validació de l'API key contra `/quotes/v1/consum-actual`.

**Acceptació**
- [ ] Un punt fora de Catalunya dona `location_outside_catalonia` i ofereix el desplegable
- [ ] Dues entrades per a la mateixa comarca → `already_configured`
- [ ] API key invàlida → error de formulari, no crea l'entrada
- [ ] Sense API key l'entrada es crea igualment i funciona
- [ ] El títol de l'entrada és el nom de la comarca

**Verificació:** `pytest tests/test_config_flow.py`; alta manual en una instància HA de
desenvolupament. **Dependències:** T6, T5. **Mida:** L

### Task 12 — Options flow, reauth i reconfigure

Options flow amb tots els camps de la passa 2 excepte l'API key; `async_step_reauth` quan
la font oficial retorna `403`; `async_step_reconfigure` per moure la ubicació.
Sense `self.config_entry` manual a l'OptionsFlow (deprecat).

**Acceptació**
- [ ] Canviar `meteors` a les opcions recarrega l'entrada i ajusta les entitats
- [ ] Un `403` en un poll obre el flux de reauth a la UI
- [ ] `reconfigure` canvia de comarca sense perdre l'historial de les entitats que
      sobreviuen

**Verificació:** `pytest tests/test_options_flow.py`. **Dependències:** T11. **Mida:** M

### Checkpoint 2 — Slice vertical
- [ ] Instal·lació manual en una instància HA: entrada creada sense clau, `nivell_d_avis`
      amb valor real i events visibles a **Eines de desenvolupament → Events**
- [ ] Cobertura ≥ 95%

---

## Fase 3 — Complements i polidura

### Task 13 — Diagnosi i resiliència

Entitats de diagnòstic del §3.12, `diagnostics.py` amb redacció de `latitude`/`longitude`/
`api_key`, comptador de fallades consecutives, `avisoscat_service_degraded` +
*repair issue*, i l'interval adaptatiu per quota (§6 de `03-feature-spec.md`).

**Acceptació**
- [ ] 3 `SmpParseError` seguits → un sol event i una *repair issue* amb `learn_more_url`
- [ ] La 4a fallada **no** repeteix l'event
- [ ] `diagnostics` no conté mai coordenades ni la clau
- [ ] Amb `maxConsultes: 100` l'interval efectiu passa a 8 h i el config flow adverteix que
      el temps violent no arribarà a temps

**Verificació:** `pytest tests/test_resilience.py tests/test_diagnostics.py`.
**Dependències:** T7. **Mida:** M

### Task 14 — Traduccions

`strings.json` + `translations/{ca,es,en}.json` amb clau per a **cada** entitat, camp de
config flow, opció de selector, error i *repair issue*. Català com a referència.

**Acceptació**
- [ ] `hassfest` verd
- [ ] `test_translations.py` comprova que els tres fitxers tenen exactament el mateix
      conjunt de claus

**Verificació:** `pytest tests/test_translations.py`; `validate.yml`.
**Dependències:** T8–T13. **Mida:** M

### Task 15 — Blueprint

`blueprints/automation/avisoscat_warning_notification.yaml` amb les opcions del §5 de
`03-feature-spec.md`, i un test que en valida l'esquema YAML (com `test_blueprint.py` de
`ha-incendiscat`).

**Acceptació**
- [ ] S'importa sense errors a una instància real
- [ ] Els filtres per meteor i per grau mínim funcionen
- [ ] `notify_on: anunciat` no notifica en entrar en vigor, i a l'inrevés
- [ ] `max_hores_antelacio` descarta els anuncis massa llunyans
- [ ] El text distingeix anunci ("d'aquí a 41 h") d'entrada en vigor
- [ ] `critical_alert` genera el payload correcte per a l'app mòbil

**Verificació:** `pytest tests/test_blueprint.py` + import manual.
**Dependències:** T7, T10. **Mida:** M

### Task 16 — README, marca i release

README en català amb instal·lació, taula d'entitats, taula d'events amb payloads, dashboard
d'exemple, patrons d'automació, **secció de coexistència amb `figorr/meteocat`**, fonts de
dades i descàrrec de responsabilitat. `brand/icon.png` 256×256. `release-please`
configurat. Sol·licitud d'alta a HACS default.

**Acceptació**
- [ ] El README diu explícitament que la integració **no està afiliada** al Meteocat ni a
      la Generalitat, i que la font sense clau no és una API oficialment suportada
- [ ] Adverteix de no fer servir `allow_html` amb `comentari`/`llindar`
- [ ] Explica que els avisos són **per comarca** i què implica
- [ ] **No diu "temps real" a seques**: explica que els avisos arriben amb hores o dies
      d'antelació i que només el temps violent és nowcast de minuts
- [ ] Documenta el cas límit del sondeig adaptatiu (fins a 30 min de retard en la primera
      vigilància d'un dia sense episodis)
- [ ] Esmenta `ha-cecat` com a integració germana per als plans de Protecció Civil
- [ ] Validació HACS verda

**Dependències:** tot. **Mida:** M

---

## Checkpoint final

- [ ] `ruff check .`, `ruff format --check .`,
      `pytest --cov=custom_components/avisoscat --cov-fail-under=95` en verd
- [ ] `hassfest` i validació HACS verdes
- [ ] Provat en una instància real durant **un episodi d'avís real**, verificant per separat
      l'anunci (hores o dies abans) i l'entrada en vigor (canvi de franja)
- [ ] Convivència verificada amb `figorr/meteocat` i amb `ha-incendiscat` instal·lats
      alhora
- [ ] Un nowcast de temps violent real detectat i notificat dins de la seva finestra de 2 h
      (l'única prova que valida el camí urgent de punta a punta)

## Fora d'abast (post-v1)

- **Plans de Protecció Civil (CECAT)** → `ha-cecat`, integració germana i separada. Font ja
  investigada a `01-data-sources.md` §5; raonament a `02-existing-integrations.md` §8. És
  petita (un endpoint Socrata, sense clau, sense quota) i hauria de cobrir **tots** els
  plans, no només els meteorològics.
- Entitat `weather` amb predicció municipal (ja la cobreix `figorr/meteocat`).
- Avisos d'AEMET com a *fallback* si la font del Meteocat es tanca.
- `geo_location` amb els polígons de les comarques avisades per pintar-les al mapa.
- Històric d'episodis per respondre "quants avisos de calor hem tingut aquest estiu".
- Card Lovelace pròpia amb el mapa comarcal i el codi semafòric oficial.
