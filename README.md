# ha-avisoscat

> Integració de Home Assistant per als **avisos de temps sever del Meteocat** (Situació
> Meteorològica de Perill, SMP) a Catalunya: converteix els avisos que afecten la teva
> comarca en entitats i events per a automacions, separant l'avís **anunciat** (emès amb
> hores o dies d'antelació) de l'avís **en vigor** (la franja ja ha començat), i reaccionant
> en minuts a l'únic senyal realment urgent, el nowcast de **temps violent**.

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)
![GitHub release (latest by date)](https://img.shields.io/github/v/release/pmontp19/ha-avisoscat)
![CI](https://github.com/pmontp19/ha-avisoscat/actions/workflows/ci.yml/badge.svg)
![License](https://img.shields.io/github/license/pmontp19/ha-avisoscat)

> 📌 **Instal·lació via repositori personalitzat.** Aquesta integració s'instal·la
> afegint-la com a *repositori personalitzat* a HACS (instruccions a sota). La inclusió
> al repositori *default* de HACS queda com a possible futur, però la revisió d'una PR a
> [`hacs/default`](https://github.com/hacs/default) pot trigar mesos; el repositori
> personalitzat és la via primària avui.

## Què és un avís SMP, i què no és

El més important per fer-ne bon ús: **l'SMP no és un sistema de temps real.** La immensa
majoria d'avisos s'emeten amb **hores o dies d'antelació**; només l'Avís de Vigilància per
**Temps Violent** és un nowcast de minuts (2 h de vigència). Això condiciona tot el disseny:

| Tipus | Antelació típica | Naturalesa |
| --- | --- | --- |
| **Preavís** | 3 dies o més | Planificació |
| **Avís** | Del dia present fins al 3r dia | Predicció (el gruix del sistema) |
| **Avís Vigilància** | Hores | Curt termini |
| **Avís Vigilància per Temps Violent** | **Minuts** (2 h de vigència) | **Nowcast**, l'únic cas urgent de debò |

Per això la integració dóna **dos senyals diferents** per cada avís: un quan l'SMC l'emet
(planificació: recollir la terrassa, canviar plans) i un altre quan entra en vigor (reacció:
tancar persianes). Confondre aquests dos horitzons és l'error de disseny que aquesta
integració evita expressament.

### Els avisos són per comarca

Els avisos SMP es resolen **per comarca, mai per municipi**: el territori es divideix en 43
comarques terrestres i 12 zones marítimes. La integració resol la teva ubicació a comarca una
vegada (al flux de configuració) i segueix els avisos que l'affecten.

Dues conseqüències que cal tenir presents:

- **Un avís actiu a la teva comarca no vol dir que et caigui a sobre.** L'SMC agrupa el temps
  violent en comarques encara que "sovint afectarà només un o alguns municipis, ja que es
  tracta de fenòmens meteorològics molt locals" (text literal de l'SMC).
- **Una entrada per comarca.** Pots crear N entrades per a N comarques (casa, feina, casa
  dels pares). Cada entrada és independent, amb les seves pròpies entitats.

## Instal·lació

### Via HACS (recomanat)

1. Obriu **HACS** al panell lateral de Home Assistant.
2. Aneu a **Settings** → **Custom repositories** (en versions anteriors de HACS: menú ⋮
   → **Custom repositories**).
3. Al camp de text, enganxeu `https://github.com/pmontp19/ha-avisoscat`.
4. Al desplegable **Category**, seleccioneu **Integration**.
5. Premeu **Add**.
6. Torneu a la pestanya d'integracions de HACS, cerqueu **"Avisos Meteocat"** i premeu
   **Install**.
7. **Reinicieu** Home Assistant.
8. Aneu a **Configuració → Dispositius i serveis → Afegeix integració**, cerqueu
   **"Avisos Meteocat"** i seguiu el flux de configuració.

### Manual

1. Copieu `custom_components/avisoscat/` d'aquest repositori dins la carpeta `custom_components/` de la vostra instal·lació de Home Assistant.
2. Reinicieu Home Assistant.
3. Afegiu la integració des de **Configuració → Dispositius i serveis**.

## Configuració

El flux de configuració té dues passes:

1. **Ubicació** - un marcador de mapa, preomplert amb `zone.home`, resolt a comarca per
   *point-in-polygon* sobre el mapa oficial de comarques. Si el punt cau fora de Catalunya
   (o no es pot descarregar el mapa), es mostra el desplegable de les 43 comarques com a
   alternativa: mai és un carreró sense sortida. La ubicació només es fa servir per triar la
   comarca; no es guarda ni s'envia en cap més moment.

2. **Opcions** - l'afinament per comarca:

   | Camp | Tipus | Default | Notes |
   | --- | --- | --- | --- |
   | `api_key` | text (opcional) | buit | Si s'omple, es valida contra `/quotes/v1/consum-actual` i activa la font oficial. En blanc, fa servir la font pública sense clau. |
   | `meteors` | multi-select | tots | Un sensor per cada meteor seleccionat (vent, pluja, neu, mar, fred, calor...). |
   | `severe_threshold` | 1–6 | `3` | Grau a partir del qual `binary_sensor.…_avis_greu` s'encén. 3 = "Alt". |
   | `include_sea` | bool | `false` | Segueix també la zona marítima adjacent (només si la comarca en té). |
   | `scan_interval` | 10–120 min | **adaptatiu** | En blanc, sondeig adaptatiu (vegeu [Sondeig](#sondeig-i-latència)). |

Un cop configurada, **Configuració → Dispositius i serveis → Avisos Meteocat → Configurar**
obre les mateixes opcions excepte l'API key, que es rota per *reauth* quan la font oficial
retorna `403`. **Reconfigurar** reobre la passa d'ubicació per canviar de comarca sense perdre
l'historial de les entitats que sobreviuen.

## Entitats

Totes les entitats pengen d'un dispositiu per comarca anomenat **"Avisos Meteocat - {comarca}"**
(p. ex. "Avisos Meteocat - Osona").

> ℹ️ Els `entity_id` d'aquesta taula corresponen a una instància de Home Assistant configurada
> **en català** per a la comarca d'**Osona**: HA genera l'`entity_id` inicial a partir del nom
> traduït de l'entitat i del nom del dispositiu, així que en una instància en castellà o anglès,
> o per a una altra comarca, seran diferents. Si una automatització no troba l'entitat,
> comprova l'`entity_id` real a **Eines de desenvolupament → Estats**.

### Sensors de nivell

| Entitat | Descripció |
| --- | --- |
| `sensor.avisos_meteocat_osona_nivell_d_avis` | Grau més alt **en vigor ara** a la comarca (`cap`/`moderat`/`alt`/`molt_alt`). Atributs: `perill` (0–6), `meteor`, `tipus`, `llindar`, `nivell`, `periode`, `distribucio_geografica`, `comentari`, `data_inici`, `data_fi`, `data_emissio`. |
| `sensor.avisos_meteocat_osona_avisos_actius` | Nombre d'avisos en vigor ara (`state_class: measurement`). Atribut `avisos`: llista de `{meteor, perill, tipus, periode, llindar}`. |
| `sensor.avisos_meteocat_osona_avis_anunciat` | Grau més alt d'un avís **emès que encara no ha entrat en vigor**. Atributs: `perill`, `meteor`, `llindar`, `nivell`, `comenca`, `hores_per_endavant`, `dia` (`avui`/`dema`/`dema_passat`), `periode`. Un avís per a demà mou aquest sensor i deixa `nivell_d_avis` a `cap`. |
| `sensor.avisos_meteocat_osona_grau_maxim_avui` | Grau màxim previst per a avui. Atribut `graella`: grau (0–6) per a les 4 franges de 6 h. Més `perill`, `meteor`, `periode`, `nivell`, `llindar`. |
| `sensor.avisos_meteocat_osona_grau_maxim_dema` | Idem per a demà. |
| `sensor.avisos_meteocat_osona_grau_maxim_dema_passat` | Idem per a demà passat. |
| `sensor.avisos_meteocat_osona_preavis` | Grau màxim del preavís vigent a escala de Catalunya (sense comarca, sense franges). Atributs: `meteor`, `perill`, `llindar`, `data_inici`, `data_fi`, `comentari`. |

### Sensors per meteor (un per meteor seleccionat)

| Entitat | Descripció |
| --- | --- |
| `sensor.avisos_meteocat_osona_avis_de_vent` | Grau de l'avís de vent **en vigor**. Creat només si has seleccionat "vent" a les opcions. |
| `..._avis_de_pluja_30min`, `..._pluja_3h`, `..._pluja_acumulada`, `..._neu`, `..._estat_de_la_mar`, `..._fred`, `..._calor`, `..._calor_nocturna`, `..._temps_violent` | Un per meteor. Atributs del pic en vigor (`perill`, `nivell`, `llindar`, `periode`, `distribucio_geografica`, `comentari`, `data_inici`, `data_fi`) i `graus_per_periode`: les 4 franges d'avui per a aquest meteor sol. |

### Binary sensors (`device_class: safety`, `on` = perill present)

| Entitat | Descripció |
| --- | --- |
| `binary_sensor.avisos_meteocat_osona_avis_actiu` | `on` si hi ha **qualsevol** avís en vigor (grau ≥ 1). Atributs: `meteor_principal`, `perill_maxim`, `nombre_avisos`. |
| `binary_sensor.avisos_meteocat_osona_avis_greu` | `on` si hi ha un avís en vigor amb grau ≥ `severe_threshold` (per defecte 3 = "Alt"). El nowcast de temps violent també hi compta: un únic interruptor cobreix tot l'horitzó "actua ara". |
| `binary_sensor.avisos_meteocat_osona_avis_greu_anunciat` | `on` si hi ha un avís **anunciat** (futur) amb grau ≥ `severe_threshold`. L'horitzó de preparació: un avís greu per a demà encén aquest sensor però no `avis_greu`. Atributs: `comenca`, `hores_per_endavant`, `meteor`, `perill`. |
| `binary_sensor.avisos_meteocat_osona_temps_violent` | `on` mentre un nowcast de temps violent és dins la seva finestra de 2 h. S'apaga sol en passar el termini, **sense cap consulta nova** (el rellotge tanca la finestra). Atributs: `probabilitat` (`alta`/`mitjana`), `llindar`, `data_emissio`, `valid_fins`. |

No hi ha entitats de diagnòstic separades: l'estat de la font es descarrega via la plataforma
**Diagnostics** (**Configuració → Dispositius i serveis → Avisos Meteocat → Descarrega la
diagnosi**), que redacta `latitude`, `longitude` i `api_key`. Quan la font falla de forma
persistent, la integració obre una **incidència de reparació** nativa (repair issue) amb un
enllaç a aquest repositori i manté les dades de l'última consulta bona.

## Events

Es disparen a `hass.bus` per fer-los servir en automacions (`trigger: event`). Cada un cobreix
un dels dos horitzons: `*_announced` és el senyal de planificació (hores o dies); els altres
són senyals en vigor; `*_violent_weather` és l'únic nowcast de minuts.

| Event | Quan es dispara |
| --- | --- |
| `avisoscat_warning_announced` | L'SMC emet o amplía un avís que encara no és en vigor (un per meteor i emissió). |
| `avisoscat_warning_started` | Un avís entra en vigor al canvi de franja de 6 h. |
| `avisoscat_warning_upgraded` | Un avís en vigor puja de grau. |
| `avisoscat_warning_downgraded` | Un avís en vigor baixa de grau. |
| `avisoscat_warning_cleared` | Un avís deixa d'estar en vigor (per rellotge o perquè la font l'ha retirat). |
| `avisoscat_violent_weather` | L'SMC emet un nowcast de temps violent (vigència ~2 h). Un per emissió. |

A més, `avisoscat_service_degraded` es dispara **un cop** quan la font falla de forma
persistent (3 errors seguits del mateix tipus) i es repeteix quan la font es repunta i torna a
fallar; va acompanyat d'una incidència de reparació nativa.

Payload de `avisoscat_warning_announced`:

```json
{
  "comarca": "Osona",
  "id_comarca": 26,
  "meteor": "vent",
  "meteor_nom": "Vent",
  "tipus": "avis",
  "perill": 4,
  "nivell_text": "alt",
  "nivell": 2,
  "llindar": "Mitjana del vent > 70 km/h, màximes > 90 km/h",
  "comenca": "2026-08-14T12:00:00+02:00",
  "hores_per_endavant": 17,
  "dia": "dema",
  "periode": "12-18 UTC",
  "distribucio_geografica": "Pirineu i Prepirineu",
  "comentari": "Vent del sud fort amb ratxes...",
  "data_emissio": "2026-08-13T19:00:00+02:00",
  "data_inici": "2026-08-14T12:00:00+02:00",
  "data_fi": "2026-08-14T18:00:00+02:00"
}
```

Payload de `avisoscat_warning_started`:

```json
{
  "comarca": "Osona",
  "id_comarca": 26,
  "meteor": "vent",
  "meteor_nom": "Vent",
  "tipus": "avis",
  "perill": 4,
  "nivell_text": "alt",
  "nivell": 2,
  "llindar": "Mitjana del vent > 70 km/h, màximes > 90 km/h",
  "periode": "12-18 UTC",
  "distribucio_geografica": "Pirineu i Prepirineu",
  "comentari": "Vent del sud fort amb ratxes...",
  "data_inici": "2026-08-14T12:00:00+02:00",
  "data_fi": "2026-08-14T18:00:00+02:00",
  "data_emissio": "2026-08-13T19:00:00+02:00",
  "anunciat_amb_hores": true
}
```

Payload de `avisoscat_warning_upgraded` / `avisoscat_warning_downgraded`:

```json
{
  "comarca": "Osona",
  "id_comarca": 26,
  "meteor": "vent",
  "perill_anterior": 3,
  "perill": 4,
  "nivell_text_anterior": "alt",
  "nivell_text": "alt",
  "periode": "12-18 UTC",
  "llindar": "Mitjana del vent > 70 km/h, màximes > 90 km/h"
}
```

Payload de `avisoscat_warning_cleared`:

```json
{
  "comarca": "Osona",
  "id_comarca": 26,
  "meteor": "vent",
  "perill_final": 4,
  "durada_min": 360,
  "motiu": "expirat"
}
```

Payload de `avisoscat_violent_weather`:

```json
{
  "comarca": "Osona",
  "id_comarca": 26,
  "probabilitat": "alta",
  "llindar": "Temps violent: pedra o ratxes extremes",
  "comentari": "Possible formació de supercèl·lules...",
  "data_emissio": "2026-08-13T15:30:00+02:00",
  "valid_fins": "2026-08-13T17:30:00+02:00"
}
```

> ⚠️ `comentari`, `llindar` i `meteor_nom` són **text extern** que prové del servei del
> Meteocat. Mai no els renderitzis amb `allow_html: true` en una targeta personalitzada (p.
> ex. Markdown card): tracta'ls com a text pla.

## Blueprint

La integració inclou un blueprint de notificacions a
[`blueprints/automation/avisoscat_warning_notification.yaml`](blueprints/automation/avisoscat_warning_notification.yaml)
que cobreix els sis events de la taula anterior, distingint els dos horitzons: l'anunciat diu
"d'aquí a N h" (planificació) i el de entrada en vigor no diu l'antelació.

[![Open your Home Assistant instance and show the blueprint import dialog.](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fraw.githubusercontent.com%2Fpmontp19%2Fha-avisoscat%2Fmain%2Fblueprints%2Fautomation%2Favisoscat_warning_notification.yaml)

Manualment: **Configuració → Automacions i escenes → Blueprints → Importa un blueprint** i
enganxeu l'URL anterior.

Opcions principals del blueprint:

| Camp | Descripció |
| --- | --- |
| `notification_service` | Servei de notificació (`notify.notify` per defecte). |
| `meteors` | Quins meteors notificar (per defecte, tots). |
| `minimum_perill` | Grau mínim per notificar (0–6; 3 = "Alt"). No s'aplica als events de resolució. |
| `notify_on` | `anunciat`, `en_vigor` o `tots dos`. El temps violent es controla a part. |
| `max_hores_antelacio` | Descarta els anuncis a més de N hores vista (evita l'avís de divendres emès dimarts). 0 = sense límit. |
| `include_upgrades` | Notifica també pujades i baixades de grau. |
| `include_cleared` | Notifica també les resolucions. |
| `include_violent_weather` | Notifica els nowcasts de temps violent (horitzó apart, sense fase d'anunci). |
| `critical_alert` | Notificació crítica que travessa el mode No molestar (recomanat només per al temps violent). |

## Dashboard d'exemple

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
      **{{ s }}** - {% for k, v in g.items() %}{{ k }}: {{ v }}{% if not loop.last %} · {% endif %}{% endfor %}
      {% endfor %}
```

> ⚠️ El `comentari` i el `llindar` són text extern: **mai** `allow_html: true` en una
> Markdown card.

## Patrons d'automació

Repartits pels dos horitzons del SMP. La majoria es cobreixen directament amb el blueprint de
la secció anterior.

**Preparació (hores o dies abans - `avisoscat_warning_announced`)**

1. **Recollir la terrassa aquest vespre** - anunci de vent amb `perill >= 4` i
   `hores_per_endavant <= 24`.
2. **Avís matinal** - `grau_maxim_avui` / `grau_maxim_dema` entre les 07:00 i les 09:00, amb
   la graella per franges al missatge.
3. **Planificar la setmana** - `preavis` a `alt` → recordatori al calendari.

**Reacció (quan entra en vigor - `avisoscat_warning_started`)**

4. **Tancar persianes** quan entra en vigor un avís de vent amb `perill >= 4`, o directament
   amb `binary_sensor.…_avis_greu` passant a `on`.
5. **Onada de calor** - `avis_calor` a `alt` → encendre el climatitzador i avisar la gent gran.

**Urgència (minuts - `avisoscat_violent_weather`)**

6. **Alerta immediata de pedra o tornado** - notificació crítica que travessa el mode No
   molestar, i tancar el tendal motoritzat. És l'únic cas on la notificació crítica està
   justificada.

**Combinacions amb integracions germanes**

7. **Risc extrem d'incendi** - avís de vent alt d'`avisoscat` **i** risc del Pla Alfa ≥ 3
   d'[`ha-incendiscat`](https://github.com/pmontp19/ha-incendiscat).
8. **Escalada real de Protecció Civil** - creuar `avis_greu` amb l'INUNCAT en fase `ALERTA`
   de l'integració germana [`ha-cecat`](https://github.com/pmontp19/ha-cecat) (plans de
   Protecció Civil: SISMICAT, INUNCAT, VENTCAT...). Dues integracions, una sola condició.

## Coexistència amb `figorr/meteocat`

[`figorr/meteocat`](https://github.com/figorr/meteocat) és una integració HACS *default* molt
establerta i completament vàlida. No cal triar: **es poden tenir instal·lades alhora** sense
conflicte (dominis diferents, entitats diferents).

La diferència és l'**abast**, no la qualitat:

| | `figorr/meteocat` | `ha-avisoscat` |
| --- | --- | --- |
| Objectiu | Integració general del Meteocat: predicció, estacions XEMA, avisos | Només els avisos SMP, amb el detall dels dos horitzons |
| Avisos | Un sensor d'avís, entre altres coses | L'eix central: anunci vs. en vigor, graella per dies/franges, un sensor per meteor, events per automatitzar |
| Predicció / XEMA | Sí | No (fora d'abast v1) |

En poques paraules: **si vols predicció i dades d'estació, `figorr/meteocat`; si vols
automatitzar al detall els avisos (inclòs el nowcast de temps violent), `ha-avisoscat`.**
Molts usuaris les tindran totes dues.

## Sondeig i latència

La integració sondeja `meteo.cat`. La cadència depèn de la font i de si hi ha algun episodi
obert, perquè els dos horitzons del SMP necessiten velocitats molt diferents:

**Font pública (per defecte, sense clau): sondeig adaptatiu**

| Situació | Interval |
| --- | --- |
| Cap episodi obert | **30 min** |
| Alguna episodi obert | **10 min** (terra nostre, més conservador que el `max-age=180` de la font) |

El canvi a 10 min només es justifica pel nowcast de temps violent, que només apareix durant
situacions convectives (que porten sempre algun episodi obert). Quan el cel està net no hi ha
res a detectar amb urgència.

> ⚠️ **Cas límit acceptat conscientment.** Un Avís de Vigilància emès per a una zona **sense
> cap episodi previ** es pot detectar **fins a 30 minuts tard** (perquè el sondeig llavors és
> de 30 min). Passar a 10 min fixos ho evitaria a canvi de triplicar la càrrega sobre
> `meteo.cat` tot l'any. Si prefereixes no acceptar aquest retard, fixa `scan_interval: 10`
> a les opcions.

**Amb API key: limitat per la quota**

| Quota mensual del pla | Interval |
| --- | --- |
| > 500 consultes | 30 min (~48 peticions/dia) |
| 200–500 | 2 h (~12 peticions/dia) |
| ≤ 200 (pla ciutadà) | 8 h (~3 peticions/dia) |

Amb quota ciutadana el nowcast de temps violent és **inservible** (8 h d'interval per a un
avís que dura 2 h). El flux de configuració ho adverteix en validar la clau i recomana la font
pública per al temps violent. La quota es llegeix un cop i l'interval s'ajusta sol.

**Recàlcul local sense xarxa.** La vigència es recalcula **cada minut**, no cada consulta: les
franges de 6 h activen i desactiven avisos sense cap canvi a la font, i un
`async_track_time_change` reprojecta la dada ja descarregada i dispara `started` / `cleared`
**sense cap petició HTTP**. Això és el que fa acceptable el sondeig lent: la font només porta
*quins avisos hi ha*; *quan entren en vigor* ja ho sabem de la dada que tenim.

## Fonts de dades

| Font | Ús | Notes |
| --- | --- | --- |
| `meteo.cat` pàgina pública (sense clau) | Font per defecte. El payload inline de la pàgina d'observacions, amb la pàgina principal com a *fallback*. | **No és una API oficialment suportada**: pot canviar d'esquema o d'adreça sense avís. |
| `api.meteo.cat` (amb clau pròpia) | Font opcional. Endpoints oficials `smp/episodis-oberts`, `preavisos` i `quotes`. | Subjecta a la quota del teu pla. La clau es guarda a `entry.data` i es rota per reauth. |
| `static-m.meteo.cat` - TopoJSON de comarques | Resoldre la ubicació a comarca al flux de configuració. | Es descarrega **un sol cop** (mai en funcionament). |

**Cap de les dues fonts d'avisos és una API oficialment suportada per a aquest ús.** La sense
clau és un payload inline d'una pàgina web; l'oficial amb clau està pensada per a altres
consumidors. Quan alguna falla:

- La integració **manté les dades de l'última consulta bona** (no les esborra; les entitats no
  passen a `unavailable` per un timeout transitori).
- Després de 3 errors seguits del mateix tipus, es dispara `avisoscat_service_degraded` i
  s'obre una **incidència de reparació** nativa amb enllaç a [GitHub Issues](https://github.com/pmontp19/ha-avisoscat/issues).
- La diagnosi descarregable redacta `latitude`, `longitude` i `api_key`.

Detalls tècnics complets (esquemes, *glossari*, *endpoints*) a
[`docs/01-data-sources.md`](docs/01-data-sources.md).

## Seguretat i dades

- La ubicació de l'usuari només surt de la instal·lació per resoldre's a comarca **una vegada**
  durant el flux de configuració (descàrrega del TopoJSON de comarques). En funcionament, la
  integració consulta avisos **per comarca**, no per coordenades.
- La diagnosi redacta `latitude`, `longitude` i `api_key` abans d'exportar-se.
- `comentari`, `llindar`, `meteor_nom` i `distribucio_geografica` provenen d'un servei extern
  no oficial i són **text no fiable**: no els renderitzis amb `allow_html: true` (p. ex. en
  una Markdown card).

## Desenvolupament

```bash
uv venv --python 3.13 .venv
uv pip install --python .venv/bin/python -r requirements_dev.txt

.venv/bin/pytest
.venv/bin/ruff check .
.venv/bin/ruff format --check .
```

Documentació d'arquitectura i disseny a [`docs/`](docs/): [fonts de dades](docs/01-data-sources.md),
[integracions existents](docs/02-existing-integrations.md), [especificació funcional](docs/03-feature-spec.md),
[arquitectura](docs/04-architecture.md), [pla d'implementació](docs/05-implementation-plan.md).

Voleu contribuir? Mireu [`CONTRIBUTING.md`](CONTRIBUTING.md) (convenció de commits, cicle de
release, tests, porta de cobertura).

## Integracions germanes

- [`ha-incendiscat`](https://github.com/pmontp19/ha-incendiscat) - incendis forestals i Pla
  Alfa (Agents Rurals). Comparteix convencions d'enginyeria amb aquesta integració.
- [`ha-cecat`](https://github.com/pmontp19/ha-cecat) - plans de Protecció Civil de Catalunya
  (INUNCAT, VENTCAT, SISMICAT...). L'encaix natural per creuar "avís greu" amb "fase
  d'activació del pla" (vegeu el patró 8).

## Descàrrec

Aquest projecte **no està afiliat ni aprovat** pel Servei Meteorològic de Catalunya ni per la
Generalitat de Catalunya. Les dades són propietat del Servei Meteorològic de Catalunya
([avís legal](https://www.meteo.cat/wpweb/avis-legal/)).

La font sense clau (el payload inline de `meteo.cat`) **no és una API oficialment suportada**:
és una càrrega de pàgina pública que pot canviar d'esquema o d'adreça sense avís. La
integració llegeix els camps amb valors per defecte i manté l'últim estat bo quan falla, però
no es pot garantir continuïtat si la font canvia de forma.

Aquesta integració no substitueix mai les alertes oficials dels canals de Protecció Civil.

## Llicència

[MIT](LICENSE).
