# Feature spec — `ha-avisoscat`

Especificació funcional de la integració d'avisos de temps sever del Meteocat.
Deriva de [`01-data-sources.md`](01-data-sources.md) i [`02-existing-integrations.md`](02-existing-integrations.md).

---

## 1. Visió general

**Nom d'usuari:** "Avisos Meteocat" · **Domini HA:** `avisoscat` · **Repositori:** `ha-avisoscat`

Segueix els **avisos de Situació Meteorològica de Perill (SMP)** que afecten la comarca de
l'usuari i els converteix en entitats i events per a automacions: recollir la terrassa el
vespre abans d'una ventada anunciada, tancar persianes quan l'avís entra en vigor, avisar
la gent gran en una onada de calor, o reaccionar en minuts a un nowcast de pedra.

### 1.1 Dos horitzons temporals, no un ⚠️

Això condiciona **tot** el disseny i és fàcil d'equivocar-se: el SMP **no és un sistema de
temps real**. La immensa majoria d'avisos s'emeten amb **hores o dies d'antelació**.

| Tipus | Antelació típica | Naturalesa |
| --- | --- | --- |
| **Preavís** | 3 dies o més | Planificació |
| **Avís** | Del dia present fins al 3r dia | **Predicció** — el gruix del sistema |
| **Avís Vigilància** | Hores | Curt termini |
| **Avís Vigilància per Temps Violent** | **Minuts** (2 h de vigència) | **Nowcast** — l'únic cas urgent de debò |

Conseqüències de disseny:

1. **Cal separar "avís emès" de "avís en vigor".** Un avís de vent emès dimarts per a dijous
   a la tarda és accionable dimarts (revisar la terrassa, canviar plans) i **també** dijous
   a les 16:00 (tancar persianes). Són dos moments diferents i necessiten dos senyals
   diferents. És exactament la distinció `advance_warning_level` / `current_warning_level`
   del `dwd_weather_warnings` de HA core.
2. **El model d'entitats ha de ser en bona part de tipus predicció**: la graella per dia i
   franja val tant o més que l'estat instantani.
3. **El sondeig agressiu només es justifica pel temps violent.** Per als avisos normals,
   30 minuts sobren; per al nowcast convectiu, 10 minuts és el mínim que permet la font.
   D'aquí el sondeig adaptatiu del §6.
4. **El README no pot dir "temps real" a seques.** Ha de dir: avisos amb dies d'antelació,
   més nowcast de temps violent amb minuts d'antelació.

### 1.2 Principis

1. **Sense fricció per defecte.** Funciona sense API key. L'API key és opcional i només
   canvia la font.
2. **Dos senyals per avís: emès i en vigor.** L'estat "en vigor" es recalcula contra el
   rellotge, perquè les franges de 6 h activen i desactiven avisos **sense cap canvi a la
   font**.
3. **Events per a automacions.** El valor diferencial respecte de `figorr/meteocat`.
4. **Honestedat territorial.** L'avís és per comarca. La UI i el README ho han de dir.
5. **Cap dependència de PyPI** (`requirements: []`), igual que `ha-incendiscat`.

### 1.3 Fora d'abast (v1)

- **Plans de Protecció Civil (CECAT)** → integració germana i separada, `ha-cecat`. El
  raonament complet és a [`02-existing-integrations.md`](02-existing-integrations.md) §8:
  àmbit territorial incompatible (Catalunya sencera vs comarca), abast natural molt més
  gran (SISMICAT, TRANSCAT, RADCAT…) i el precedent `nina` / `dwd_weather_warnings` de HA
  core. La recerca de la font es conserva a `01-data-sources.md` §5.
- Entitat `weather` amb predicció municipal → ja la cobreix bé `figorr/meteocat`.
- Dades d'estació (XEMA), llamps (XDDE), UVI.
- Avisos d'AEMET (autoritat diferent, zones diferents).

---

## 2. Config flow

Multi-entrada: es poden crear **N entrades per a N comarques** (casa, feina, casa dels
pares). A diferència d'`ha-incendiscat`, **no** es declara `single_config_entry`.
`unique_id` de l'entrada = `str(id_comarca)` (i `mar-{id}` si és una zona marítima), amb
`_abort_if_unique_id_configured()`.

### Passa 1 — Ubicació

- `LocationSelector` **sense radi**, preomplert amb `zone.home`.
- En enviar: resolució *point-in-polygon* sobre `comarquesAmbMar.json` → comarca.
- Si el punt cau fora de Catalunya: error `location_outside_catalonia` amb el desplegable
  de comarques com a alternativa.
- Alternativa sempre disponible: `SelectSelector` amb les 43 comarques, per si algú vol
  seguir una comarca on no viu.
- El títol de l'entrada serà el nom de la comarca ("Avisos Meteocat — Osona").

### Passa 2 — Opcions

| Camp | Tipus | Default | Notes |
| --- | --- | --- | --- |
| `api_key` | text (opcional) | buit | Si s'omple, es valida amb `/quotes/v1/consum-actual` i es passa a la font oficial |
| `meteors` | multi-select | tots | Quins meteors seguir (crea/omet els sensors per meteor) |
| `severe_threshold` | 1–6 | `3` | Grau a partir del qual `binary_sensor.severe_warning` s'encén (3 = "Alt") |
| `include_sea` | bool | `false` | Afegeix la zona marítima adjacent (només si la comarca en té) |
| `scan_interval` | 10–120 min | **adaptatiu** | Vegeu §6. Mínim 10 min: la font té `cache-control: max-age=600` |

Quan hi ha API key, el `scan_interval` efectiu es limita per la quota (vegeu §6).

### Options flow

Tot el de la passa 2 excepte `api_key`, que es gestiona per **reauth** quan el servidor
retorna `403`.

### Reconfiguració

`async_step_reconfigure` reobre la passa 1 per moure la ubicació / canviar de comarca.

---

## 3. Entitats

Un dispositiu per entrada, `DeviceInfo.entry_type = SERVICE`:
`name = "Avisos Meteocat — {comarca}"`, `manufacturer = "Servei Meteorològic de Catalunya"`,
`model = "Situació Meteorològica de Perill"`.

Tots els noms visibles surten de `_attr_translation_key` + `translations/{ca,es,en}.json`
(el català és la llengua de referència). Els `entity_id` que apareixen aquí corresponen a
una instància en català.

### 3.1 `sensor.avisos_meteocat_<comarca>_nivell_d_avis` ⭐

Grau més alt **vigent ara** a la comarca, projectat al codi semafòric.

- `device_class: ENUM`, `options: ["cap", "moderat", "alt", "molt_alt"]`
- `translation_key: warning_level`

| Atribut | Descripció |
| --- | --- |
| `perill` | Grau numèric 0–6 |
| `meteor` | Meteor que provoca el màxim (`vent`, `pluja_30min`, …) |
| `tipus` | `avis` / `vigilancia` / `temps_violent` |
| `llindar` | Text literal del llindar (p. ex. `Intensitat > 20 mm / 30 minuts`) |
| `nivell` | `1` (llindar baix) o `2` (llindar alt) |
| `periode` | Franja vigent (`12-18`) |
| `distribucio_geografica` | `LOCAL` / `EXTENSA` / `GENERAL` |
| `comentari` | Text del predictor. **Text extern no fiable** |
| `data_inici`, `data_fi` | ISO 8601 UTC de l'avís |
| `data_emissio` | Quan l'SMC el va emetre |

### 3.2 `sensor.avisos_meteocat_<comarca>_avisos_actius`

Nombre d'avisos vigents ara. `state_class: MEASUREMENT`, `unit: "avisos"`.
Atribut `avisos`: llista de `{meteor, perill, tipus, periode, llindar}`.

### 3.3 `sensor.avisos_meteocat_<comarca>_avis_anunciat` ⭐

L'equivalent d'`advance_warning_level` del DWD, i **la meitat del valor de la integració**
donat que el SMP avisa amb dies d'antelació (§1.1).

Grau més alt de **qualsevol avís emès que encara no ha entrat en vigor** (avui més tard,
demà o demà passat). `ENUM` igual que §3.1.

| Atribut | Descripció |
| --- | --- |
| `perill` | Grau numèric 0–6 |
| `meteor`, `llindar`, `nivell` | Del pic anunciat |
| `comenca` | ISO 8601 UTC de l'inici de la primera franja afectada |
| `hores_per_endavant` | Antelació en hores respecte d'ara |
| `dia` | `avui` / `dema` / `dema_passat` |
| `periode` | Franja del pic |

Amb això, l'automació "avisa'm quan anunciïn alguna cosa greu" és una transició d'estat, no
un càlcul sobre atributs.

### 3.4 `sensor.…_grau_maxim_avui` / `…_grau_maxim_dema` / `…_grau_maxim_dema_passat`

Grau màxim 0–6 previst per a **qualsevol franja** del dia corresponent, hi hagi o no res
vigent ara. `grau_maxim_avui` és el sensor de l'automació matinal ("avui hi ha avís de
calor a la tarda"); els altres dos cobreixen l'horitzó de predicció complet del SMP.

Atributs: `meteor`, `periode`, `nivell`, `llindar`, i `graella` amb el grau de cadascuna de
les 4 franges d'aquell dia:

```json
{ "graella": { "00-06": 0, "06-12": 0, "12-18": 2, "18-00": 3 },
  "meteor": "pluja_30min", "periode": "18-00", "nivell": 1,
  "llindar": "Intensitat > 20 mm / 30 minuts" }
```

### 3.5 `sensor.avisos_meteocat_<comarca>_avis_<meteor>` (×10) ⭐

Un per meteor, creats només per als meteors seleccionats. Mateixa forma que §3.1
(`ENUM` amb `cap`/`moderat`/`alt`/`molt_alt`) però restringit a aquell meteor.

| `translation_key` | Meteor SMP |
| --- | --- |
| `warning_wind` | Vent |
| `warning_rain_30min` | Intensitat de pluja en 30 minuts |
| `warning_rain_3h` | Intensitat de pluja en 3 hores |
| `warning_rain_accumulated` | Acumulació de pluja |
| `warning_snow` | Neu |
| `warning_sea` | Estat de la mar |
| `warning_cold` | Fred |
| `warning_heat` | Calor |
| `warning_night_heat` | Calor nocturna |
| `warning_violent_weather` | Temps violent |

Atributs: `perill`, `nivell`, `llindar`, `periode`, `distribucio_geografica`, `comentari`,
`data_inici`, `data_fi`, `graus_per_periode` (dict `{"00-06": 0, "06-12": 0, "12-18": 2,
"18-00": 1}` del dia en curs, per pintar una barra a un dashboard).

Estat `cap` quan no hi ha avís d'aquell meteor. Si el meteor arriba amb un nom que no
reconeixem, warning al log i s'ignora — mai una excepció (trap #5 de `01-data-sources.md`).

### 3.6 `sensor.avisos_meteocat_<comarca>_preavis`

Grau màxim del preavís vigent **a escala de Catalunya** (els preavisos no tenen comarca).
`ENUM` igual que la resta. Atributs: `meteor`, `perill`, `llindar`, `data_inici`,
`data_fi`, `comentari`.

### 3.7 `sensor.avisos_meteocat_<comarca>_avis_maritim`

Només si `include_sea`. Grau vigent a la zona marítima adjacent (`idComarca` 88–99).
Atributs: `zona` (`Mar Maresme`), `perill`, `llindar`, `periode`.

### 3.8 `binary_sensor.avisos_meteocat_<comarca>_avis_actiu`

`device_class: SAFETY`. `on` si hi ha algun avís **en vigor ara** (grau ≥ 1).
Atributs quan és `on`: `meteor_principal`, `perill_maxim`, `nombre_avisos`.

### 3.9 `binary_sensor.avisos_meteocat_<comarca>_avis_greu`

`device_class: SAFETY`. `on` si `perill ≥ severe_threshold` (default 3 = "Alt") **en vigor
ara**. Aquest és el disparador recomanat per a automacions de protecció immediata.

### 3.10 `binary_sensor.avisos_meteocat_<comarca>_avis_greu_anunciat`

`device_class: SAFETY`. `on` si hi ha un avís **emès però encara no vigent** amb
`perill ≥ severe_threshold`. El disparador per a automacions de preparació (§1.1).
Atributs: `comenca`, `hores_per_endavant`, `meteor`, `perill`.

### 3.11 `binary_sensor.avisos_meteocat_<comarca>_temps_violent`

`device_class: SAFETY`. `on` mentre hi hagi un **Avís de Vigilància per Temps Violent**
dins de la seva finestra de 2 h. Atributs: `probabilitat` (`alta`/`mitjana`), `llindar`,
`data_emissio`, `valid_fins`.

### 3.12 Entitats de diagnòstic

Necessàries perquè la font principal no és una API oficialment suportada.

| Entitat | Descripció |
| --- | --- |
| `binary_sensor.…_servei_connectat` | `on` si l'última consulta ha anat bé |
| `sensor.…_darrera_actualitzacio` | `device_class: TIMESTAMP` de l'última sincronització correcta |
| `sensor.…_estat_de_la_darrera_actualitzacio` | `success` o `error_<codi>` (`error_timeout`, `error_http_403`, `error_parse`) |
| `sensor.…_quota_restant` | Només amb API key: `consultesRestants` del pla de Predicció. Atributs `max_consultes`, `consultes_realitzades`, `periode` |

Diagnostics (`diagnostics.py`) ha de **redactar** `latitude`, `longitude` i `api_key`.

---

## 4. Events

El motiu de ser de la integració. Es disparen a `hass.bus` i s'usen amb `trigger: event`.

**Els events segueixen els dos horitzons del §1.1**: `announced` cobreix l'emissió (pot ser
d'aquí a tres dies), `started` cobreix l'entrada en vigor. Confondre'ls és l'error de
disseny més fàcil de cometre aquí: si només tinguéssim `started`, un avís de vent emès
dimarts per a dijous no generaria cap senyal fins dijous a la tarda, quan ja no hi ha temps
de fer-hi res.

| Event | Quan | Horitzó |
| --- | --- | --- |
| `avisoscat_warning_announced` | L'SMC emet (o amplia) un avís que afectarà la comarca | Hores a dies |
| `avisoscat_warning_started` | L'avís entra en vigor | Ara |
| `avisoscat_warning_upgraded` / `_downgraded` | Canvia el grau d'un avís en vigor | Ara |
| `avisoscat_warning_cleared` | L'avís deixa d'estar en vigor | Ara |
| `avisoscat_violent_weather` | Nowcast de temps violent | Minuts |
| `avisoscat_service_degraded` | La font falla de forma persistent | — |

### 4.1 `avisoscat_warning_announced` ⭐

L'SMC ha emès un avís nou, o n'ha ampliat un d'existent, que afectarà la comarca en algun
moment futur. **Aquest és el senyal de planificació** i, pel funcionament real del SMP, el
que es dispararà més sovint.

Es dispara un cop per `(meteor, tipus, data_emissio)`: una reemissió amb el mateix contingut
no el repeteix. Si l'SMC amplia un avís (`estat: "Ampliat"`), es dispara de nou amb el nou
`data_emissio`.

```json
{
  "comarca": "Osona",
  "id_comarca": 24,
  "meteor": "vent",
  "meteor_nom": "Vent",
  "tipus": "avis",
  "perill": 4,
  "nivell_text": "alt",
  "nivell": 2,
  "llindar": "Ratxa màxima > 108 km/h (30 m/s)",
  "comenca": "2026-08-07T16:00:00+00:00",
  "hores_per_endavant": 41,
  "dia": "dema_passat",
  "periode": "12-18",
  "distribucio_geografica": "EXTENSA",
  "comentari": "Ratxes molt fortes al litoral.",
  "data_emissio": "2026-08-05T23:00:00+00:00",
  "data_inici": "2026-08-07T12:00:00+00:00",
  "data_fi": "2026-08-07T23:59:00+00:00"
}
```

### 4.2 `avisoscat_warning_started`

Un avís entra en vigor per a la comarca: comença la franja afectada. **No implica cap dada
nova de la font** — normalment el dispara el canvi de franja, calculat contra el rellotge
(§6).

```json
{
  "comarca": "Osona",
  "id_comarca": 24,
  "meteor": "pluja_30min",
  "meteor_nom": "Intensitat de pluja en 30 minuts",
  "tipus": "avis",
  "perill": 3,
  "nivell_text": "alt",
  "nivell": 1,
  "llindar": "Intensitat > 20 mm / 30 minuts",
  "periode": "12-18",
  "distribucio_geografica": "LOCAL",
  "comentari": "Els xàfecs aniran acompanyats de tempesta.",
  "data_inici": "2026-08-04T12:00:00+00:00",
  "data_fi": "2026-08-06T17:59:00+00:00",
  "data_emissio": "2026-08-04T15:30:00+00:00",
  "anunciat_amb_hores": 20
}
```

`anunciat_amb_hores`: antelació real entre `data_emissio` i l'entrada en vigor. Útil per
distingir un avís planificat d'una vigilància de darrera hora dins de la mateixa automació.

### 4.3 `avisoscat_warning_upgraded` / `avisoscat_warning_downgraded`

El grau d'un avís **ja en vigor** puja o baixa (inclou el canvi de franja que en modifica el
grau).

```json
{
  "comarca": "Osona",
  "id_comarca": 24,
  "meteor": "vent",
  "perill_anterior": 2,
  "perill": 4,
  "nivell_text_anterior": "moderat",
  "nivell_text": "alt",
  "periode": "18-00",
  "llindar": "Ratxa màxima > 108 km/h (30 m/s)"
}
```

### 4.4 `avisoscat_warning_cleared`

Un avís en vigor deixa de ser-ho (final de franja, final de l'avís, o l'SMC el tanca).

```json
{
  "comarca": "Osona",
  "id_comarca": 24,
  "meteor": "vent",
  "perill_final": 4,
  "durada_min": 372,
  "motiu": "expirat"
}
```

`motiu`: `expirat` (`dataFi` superada o franja acabada) o `retirat` (ha desaparegut de la
font abans d'hora).

### 4.5 `avisoscat_violent_weather` ⭐

Avís de Vigilància per Temps Violent que afecta la comarca. **L'únic event genuïnament de
temps real** del sistema: 2 h de vigència, emès a partir del *lightning jump* observat per
la XDDE, amb minuts d'antelació.

No té fase d'anunci: quan s'emet, ja està en vigor.

```json
{
  "comarca": "Osona",
  "id_comarca": 24,
  "probabilitat": "alta",
  "llindar": "Pedra de diàmetre > 2 cm",
  "comentari": "",
  "data_emissio": "2026-08-05T16:12:00+00:00",
  "valid_fins": "2026-08-05T18:12:00+00:00"
}
```

### 4.6 `avisoscat_service_degraded`

La font falla de forma persistent (3 cicles seguits amb el mateix tipus d'error, p. ex.
el payload ja no es pot extreure de la pàgina, o `403` repetit). Es crea també una
*repair issue* amb enllaç al repositori.

```json
{ "source": "meteocat_public", "error": "parse", "consecutive_failures": 3 }
```

---

## 5. Blueprint

`blueprints/automation/avisoscat_warning_notification.yaml`:

| Camp | Tipus | Descripció |
| --- | --- | --- |
| `notification_service` | selector | Servei de notificació (`notify.notify` per defecte) |
| `meteors` | multi-select | Quins meteors notificar (tots per defecte) |
| `minimum_perill` | 1–6 | Grau mínim per notificar (3 per defecte) |
| `notify_on` | select | `anunciat` / `en_vigor` / `tots dos`. Per defecte **tots dos**: un missatge de preparació quan s'emet i un altre quan entra en vigor |
| `max_hores_antelacio` | int | Ignorar anuncis a més de N hores vista (0 = sense límit). Evita la notificació de dimarts per a un avís de divendres |
| `include_upgrades` | bool | Notificar també quan un avís puja de grau |
| `include_cleared` | bool | Notificar quan es resol |
| `include_violent_weather` | bool | Notificar `avisoscat_violent_weather` (per defecte sí) |
| `critical_alert` | bool | Notificació crítica que travessa el mode No molestar. **Recomanat només per a temps violent** |

El text de la notificació ha de dir l'antelació quan es tracta d'un anunci
("Avís de vent alt d'aquí a 41 h") i no dir-la quan ja és en vigor.

---

## 6. Estratègia de sondeig

Els dos horitzons del §1.1 tenen necessitats de sondeig molt diferents, i tractar-los igual
seria o bé massa lent per al temps violent o bé massa agressiu contra un servei públic
durant els ~300 dies l'any en què no passa res.

### Font pública (per defecte): adaptatiu

| Situació | Interval |
| --- | --- |
| Cap episodi obert | **30 min** |
| Algun episodi obert | **10 min** (el mínim que permet `max-age=600`) |

El 10 min només es justifica pel nowcast de temps violent, i el temps violent només
apareix durant situacions convectives, que porten sempre algun episodi obert. Quan el cel
està net no hi ha res a detectar amb urgència.

⚠️ **Cas límit acceptat conscientment**: un Avís de Vigilància emès per a una zona sense cap
episodi previ es pot detectar fins a 30 minuts tard. Passar a 10 min fixos ho evitaria a
canvi de triplicar la càrrega sobre `meteo.cat` tot l'any. Es documenta al README i
`scan_interval` permet forçar 10 min fixos a qui ho prefereixi.

### Amb API key: limitat per la quota

| Quota mensual del pla | Interval |
| --- | --- |
| > 500 | 30 min (~48 peticions/dia) |
| 200–500 | 2 h (~12 peticions/dia) |
| ≤ 200 | 8 h (~3 peticions/dia — el límit real del pla ciutadà) |

Es llegeix `/quotes/v1/consum-actual` **un cop al dia** i l'interval s'ajusta sol. Si
`consultesRestants` baixa del 10%, l'interval es duplica i s'emet un warning al log.

Amb quota ciutadana el nowcast de temps violent és **inservible** (8 h d'interval per a un
avís que dura 2 h). El config flow ho ha d'advertir en validar la clau: *amb aquesta quota,
els avisos de temps violent no arribaran a temps; considera fer servir la font pública*.

### Recàlcul local sense xarxa

⚠️ **La vigència es recalcula cada minut, no cada poll.** Les franges de 6 h canvien sense
que canviï la font: un `async_track_time_change` cada minut recalcula quins avisos són en
vigor i dispara `avisoscat_warning_started` / `_cleared` **sense cap petició HTTP**.

Això és el que fa que el sondeig lent sigui acceptable: la font només ha de portar *quins
avisos hi ha*; *quan entren en vigor* ja ho sabem de la dada que tenim.

---

## 7. Dashboard d'exemple

```yaml
type: vertical-stack
cards:
  - type: glance
    entities:
      - sensor.avisos_meteocat_osona_nivell_d_avis
      - sensor.avisos_meteocat_osona_avis_anunciat
      - sensor.avisos_meteocat_osona_grau_maxim_avui
      - sensor.avisos_meteocat_osona_grau_maxim_dema
      - binary_sensor.avisos_meteocat_osona_avis_greu
  - type: markdown
    title: Avisos en vigor
    content: |
      {% set a = state_attr('sensor.avisos_meteocat_osona_avisos_actius','avisos') or [] %}
      {% if a | count == 0 %}_Cap avís en vigor._{% else %}
      | Meteor | Grau | Franja | Llindar |
      |:--|--:|:--|:--|
      {% for w in a %}
      | {{ w.meteor }} | {{ w.perill }} | {{ w.periode }} | {{ w.llindar }} |
      {% endfor %}
      {% endif %}
  - type: markdown
    title: Previsió d'avisos
    content: |
      {% for s in ['avui','dema','dema_passat'] %}
      {% set g = state_attr('sensor.avisos_meteocat_osona_grau_maxim_' ~ s,'graella') or {} %}
      **{{ s }}** — {% for k, v in g.items() %}{{ k }}: {{ v }}{% if not loop.last %} · {% endif %}{% endfor %}
      {% endfor %}
```

> El `comentari` i el `llindar` són text extern: mai `allow_html: true` en una Markdown card.

---

## 8. Patrons d'automació

Repartits pels dos horitzons del §1.1.

**Preparació (hores o dies abans, amb `avisoscat_warning_announced`)**

1. **Recollir la terrassa aquest vespre**: anunci amb `meteor: vent`, `perill >= 4` i
   `hores_per_endavant <= 24` → notificació amb l'hora d'inici prevista.
2. **Avís matinal**: `sensor.…_grau_maxim_avui` i `…_grau_maxim_dema` entre les 07:00 i les
   09:00, amb la graella per franges al missatge.
3. **Planificar la setmana**: `sensor.…_preavis` a `alt` → recordatori al calendari.

**Reacció (quan entra en vigor, amb `avisoscat_warning_started`)**

4. **Tancar persianes** quan entra en vigor un avís de vent amb `perill >= 4`.
5. **Onada de calor**: `sensor.…_avis_calor` a `alt` → encendre el climatitzador i avisar
   la gent gran de casa.

**Urgència (minuts, amb `avisoscat_violent_weather`)**

6. **Alerta immediata de pedra o tornado** → notificació crítica que travessa el mode No
   molestar, i tancar el tendal motoritzat. És l'únic cas on la notificació crítica està
   justificada.

**Combinacions**

7. **Risc extrem d'incendi**: avís de vent alt d'`avisoscat` **i** risc del Pla Alfa ≥ 3
   d'`ha-incendiscat`.
8. **Escalada real**: quan existeixi la integració germana `ha-cecat`, creuar
   `binary_sensor.…_avis_greu` amb l'INUNCAT en fase `ALERTA` — dues integracions, una sola
   condició d'automació.
