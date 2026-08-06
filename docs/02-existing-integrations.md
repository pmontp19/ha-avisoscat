# Integracions de referència — avisos meteorològics

Anàlisi de què hi ha ja per a Catalunya i Espanya, i de quins patrons val la pena copiar.
Recerca feta el 2026-08-05 (llista `hacs/default`, HA core `dev`, codi font llegit
directament).

---

## 1. Panorama

| Integració | Distribució | Estat | Cobreix avisos SMP? |
| --- | --- | --- | :---: |
| [`figorr/meteocat`](https://github.com/figorr/meteocat) | **HACS default** | Activa (v4.2.0) | **Parcialment** |
| [`meteoalarm`](https://www.home-assistant.io/integrations/meteoalarm/) | HA core | `quality_scale: legacy`, només YAML | No (EUMETNET/AEMET) |
| [`aemet`](https://www.home-assistant.io/integrations/aemet/) | HA core | Activa | No |
| [`geosphere_austria_warnings`](https://www.home-assistant.io/integrations/geosphere_austria_warnings) | HA core | Activa, `bronze` | — (Àustria) |
| [`karasu/meteocat`](https://github.com/karasu/meteocat) | GitHub | Abandonada | No |
| [`Danieldiazi/homeassistant-meteogalicia`](https://github.com/Danieldiazi/homeassistant-meteogalicia) | HACS default | Activa | — (Galícia) |

**Conclusió: no existeix cap integració d'avisos SMP orientada a automacions en temps
real.** El buit és real.

---

## 2. `figorr/meteocat` — el competidor directe

L'única integració que consumeix `api.meteo.cat`. És a la llista **HACS default**, molt
completa i mantinguda. Cal ser honestos: fa moltes coses bé i **no volem duplicar-la**.

### Què fa

Requereix API key amb **tres plans** simultanis (`Dades de la XEMA`, `Dades de la XDDE`,
`Dades de Predicció`). El config flow demana clau → municipi → estació XEMA → límits de
quota → àrea. Genera sensors d'observació (XEMA), predicció municipal, UVI, llamps (XDDE),
ETo, quotes i **alertes SMP**.

Dependències externes: `meteocatpy`, `solarmoonpy`, `gardenpy-meteocat`, `packaging`,
`wrapt`. `dependencies: ["persistent_notification", "http"]`. Cache en fitxers JSON al disc.

### Com exposa els avisos SMP

Dos tipus de sensor, ambdós lligats al `region_id` (comarca) del municipi triat:

`MeteocatAlertRegionSensor` — estat = nombre d'alertes actives; atributs `alert_1`,
`alert_2`, … amb el nom normalitzat del meteor.

`MeteocatAlertMeteorSensor` — un per meteor (`alert_wind`, `alert_rain_intensity_30_min`,
`alert_rain_intensity_3_hours`, `alert_rain`, `alert_snow`, `alert_sea`, `alert_cold`,
`alert_warm`, `alert_warm_night`):

```python
@property
def native_value(self):
    ...
    estado_original = meteor_data.get("estado", "Tancat")
    return self.STATE_MAPPING.get(estado_original, "unknown")   # "opened" / "closed"

@property
def extra_state_attributes(self):
    return { "inicio": …, "fin": …, "fecha": …, "periodo": …,
             "umbral": umbral_convertido, "nivel": …, "peligro": …, "comentari": … }
```

Té taules de mapatge molt completes (`UMBRAL_MAPPING` amb ~40 llindars literals) i,
notablement, **avisa amb un warning quan troba un meteor o un llindar desconegut** en lloc
de petar — la mateixa filosofia de tolerància que volem.

### Per què no ens serveix

| Limitació | Impacte |
| --- | --- |
| **L'estat del sensor és `opened`/`closed` de l'episodi**, no el grau de perill de la teva comarca ara | `peligro` queda com a atribut. Una automació ha de fer `state_attr(...)` i comparar números en lloc de reaccionar a un canvi d'estat |
| **Sense binary sensors i sense events** | No hi ha *edge trigger* net per a "acaba d'entrar un avís greu". El patró event-driven de HA no s'aprofita |
| **Sense distinció "ara" vs "més tard avui"** | Els atributs porten franja i dia, però l'estat no diu si l'avís està vigent en aquest moment |
| **No cobreix Preavís, Avís Vigilància ni Temps Violent** | Es perd precisament l'avís més urgent (2 h de vigència, *lightning jump*) |
| **Exigeix 3 plans d'API** | No pots tenir només els avisos; instal·lar-la per als avisos t'obliga a XEMA i XDDE |
| **Temps real impossible amb quota ciutadana** | `ALERT_VALIDITY_MULTIPLIER_100 = 12` sobre `DEFAULT_ALERT_VALIDITY_TIME = 120` min → refresc **cada 24 h** quan `limit_prediccio <= 100` (el default) |
| Cache en fitxers, `http` i `persistent_notification` com a dependències, 5 deps de PyPI | Superfície de manteniment gran per a qui només vol avisos |
| Historial de *breaking changes* d'`entity_id` (v3.0.0) i `entity_id` fixats a mà | — |

### Què n'agafem

- La llista de meteors i la taula de llindars literals (§1.3 de `01-data-sources.md`).
- El patró "meteor/llindar desconegut → warning + `unknown`, mai excepció".
- El sensor de quota a partir de `/quotes/v1/consum-actual`.
- La idea de fer l'interval **dependent de la quota** quan hi ha API key.

### Coexistència

Les dues integracions han de poder conviure a la mateixa instància: dominis diferents
(`meteocat` vs `avisoscat`), `unique_id` diferents, i cap col·lisió d'`entity_id` (els seus
són `sensor.meteocat_{station_id}_{town}_{sensor}`). El README ho ha de dir explícitament:
*si vols predicció i estació XEMA, fes servir `figorr/meteocat`; això només fa avisos, i
sense clau*.

---

## 3. `geosphere_austria_warnings` (HA core) — el model a copiar ⭐

Integració d'avisos oficials d'Àustria, acceptada a core amb `quality_scale: bronze`. És
l'exemple canònic de com HA vol que es modelin els avisos meteorològics avui.

### Model d'entitats — deliberadament petit

```python
SENSORS = (
    GeoSphereSensorDescription(
        key="warning_level",
        translation_key="warning_level",
        device_class=SensorDeviceClass.ENUM,
        options=[LEVEL_NONE, "yellow", "orange", "red"],
        value_fn=_max_level,
    ),
    GeoSphereSensorDescription(
        key="active_warnings",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=len,
    ),
)
```

Dues entitats per municipi: **el nivell més alt vigent com a `SensorDeviceClass.ENUM`** i
**el recompte**. Res més. El `device_class=ENUM` amb `options` és el que fa que el nivell
sigui usable directament en automacions i targetes.

### Coordinator — precomprovació barata

```python
UPDATE_INTERVAL = timedelta(minutes=5)

async def _async_update_data(self) -> GeoSphereData:
    last_modified = await self.client.get_last_modified()
    if self.data is None or last_modified != self._last_modified:
        location_warnings = await self.client.get_warnings_for_coords(lat, lon)
    else:
        location_warnings = self.data.location_warnings
    ...
    return GeoSphereData(
        location_warnings=location_warnings,
        active_warnings=[w for w in location_warnings.warnings if w.is_active(now)],
    )
```

Dues idees clau:

1. **Precomprovació barata abans de la descàrrega completa.** Nosaltres no tenim `ETag` ni
   `Last-Modified` (verificat), així que no la podem fer igual — però sí que podem
   comparar un hash del payload extret per evitar recalcular estat i emetre events
   espuris.
2. **`is_active(now)` calculat al coordinator, no a l'entitat.** L'estat "vigent ara" es
   deriva del rellotge, no només de les dades: cal recalcular-lo encara que el payload no
   canviï. Per a nosaltres és crític, perquè les franges de 6 h fan que un avís s'activi i
   es desactivi **sense cap canvi a la font**.

### Altres detalls

`integration_type: "service"`, `iot_class: cloud_polling`, `runtime_data` amb àlies tipat,
una entry per municipi amb prevenció de duplicats, i el `const.py` documenta la llicència
de les dades. Tot això ho volem igual.

**On ens en separem**: dues entitats no basten per al SMP. El grau de perill no és
"groc/taronja/vermell" sinó una escala 0–6 que es projecta a 4 categories, i l'usuari vol
saber **quin** meteor (tancar persianes per vent ≠ recollir tendals per pedra ≠ hidratar
la gent gran per calor). Per això afegim un sensor per meteor i events al bus.

---

## 4. `meteoalarm` (HA core) — el que no s'ha de fer

Integració genèrica europea (EUMETNET) que per a Espanya serveix avisos **d'AEMET**, no del
Meteocat, a granularitat de província.

- **Només YAML** (`PLATFORM_SCHEMA` amb `country`/`province`), sense config flow.
- `quality_scale: legacy`.
- Limitació coneguda i documentada: **quan hi ha diverses alertes actives per a una regió,
  només se n'obté la primera** ([core#65699](https://github.com/home-assistant/core/issues/65699)).
- Depèn de `meteoalertapi==0.3.1`, un paquet de tercers poc mantingut.

És l'alternativa que un usuari català fa servir avui, i és clarament pitjor que llegir el
SMP directament: dades d'una altra agència, granularitat de província i una sola alerta.

---

## 5. Alemanya: `dwd_weather_warnings` + `nina` — el precedent per a la pregunta "una o dues?" ⭐

Alemanya té **les dues integracions per separat a HA core**, i és exactament el mateix
repartiment institucional que Catalunya:

| | `dwd_weather_warnings` | `nina` |
| --- | --- | --- |
| Autoritat | Deutscher Wetterdienst (servei meteorològic) | Bundesamt für Bevölkerungsschutz (protecció civil) |
| Equivalent català | **Meteocat / SMC** | **CECAT / Protecció Civil** |
| Abast | Només avisos meteorològics | Agregador: meteo (reenviada del DWD) + inundacions + substàncies perilloses + catàstrofes |
| Entrades | Una per regió | **`single_config_entry: true`** (àmbit nacional) |
| Cadència | 15 min | 5 min |
| Entitats | `current_warning_level` + **`advance_warning_level`** | Binary sensors per regió i per tipus |

Dos aprenentatges directes:

1. **La separació és la norma, no una excentricitat.** El servei meteorològic emet el que
   preveu; protecció civil decideix si activa un pla. Són autoritats, cadències i cicles de
   vida diferents, i HA core les modela per separat encara que les dades del DWD
   *també* apareguin dins de NINA.
2. **`single_config_entry` a l'agregador de protecció civil.** NINA és nacional; el DWD és
   per regió. Igual que el CECAT és per a tot Catalunya i el SMP és per comarca — vegeu
   §8.

I un aprenentatge sobre l'horitzó temporal: el DWD separa **`current_warning_level`**
(avís en vigor ara) de **`advance_warning_level`** (*Vorabinformation*, avís emès per a un
període futur). És exactament la distinció que el SMP necessita i que la primera versió
d'aquesta especificació no feia.

## 6. `caiosweet/DPC-Alert` (Itàlia) — l'altre model

Integració HACS default per al **Dipartimento della Protezione Civile** italià: nivells
d'alerta (verd/groc/taronja/vermell) per zona, per risc hidrogeològic, hidràulic i
temporals. És l'equivalent italià del CECAT, i també viu **separada** de qualsevol
integració del servei meteorològic.

Confirma el mateix patró: les integracions de protecció civil s'organitzen per *pla de
risc i zona d'emergència*, no per *meteor i comarca*.

## 7. `aemet` (HA core)

Integració oficial d'AEMET OpenData: predicció i observació, **sense avisos**. Incloure'ls
és una *feature request* oberta i no implementada.

AEMET publica avisos CAP a `opendata.aemet.es` (`/api/avisos_cap/ultimoelaborado/area/cat`)
amb una quota molt més generosa que el Meteocat. **No l'usem**: són avisos d'AEMET amb
zones pròpies, no les SMP per comarca del Meteocat, i barrejar-los confondria l'usuari
sobre quina autoritat ha emès què. Queda anotat com a possible *fallback* futur si la font
del Meteocat es tanca.

---

## 8. Decisió: Meteocat i Protecció Civil van separats

La pregunta és si el SMP del Meteocat i els plans del CECAT han d'anar en una integració o
en dues. **Dues**, i aquests són els arguments, ordenats de més a menys decisiu.

### 8.1 L'àmbit territorial no encaixa

El SMP és **per comarca** i la integració és multi-entrada (casa, feina, els pares). El
CECAT és **per a tot Catalunya**: quan s'activa l'INUNCAT, s'activa i punt. Posar
`sensor.plans_activats` dins del dispositiu d'una comarca vol dir que un usuari amb tres
entrades té **tres còpies idèntiques del mateix INUNCAT**, amb tres `unique_id` diferents i
tres events duplicats a cada canvi de fase.

Això no és un detall estètic: és el motiu pel qual `nina` declara
`single_config_entry: true` i `dwd_weather_warnings` no. Els dos àmbits són incompatibles
dins d'una mateixa entrada.

### 8.2 L'abast natural del CECAT és molt més gran que el temps

El dataset `wj9c-j6vf` cobreix **tots** els plans de Protecció Civil, no només els
meteorològics: INUNCAT (inundacions), VENTCAT (vent), NEUCAT (neu), però també PROCICAT,
SISMICAT, TRANSCAT (mercaderies perilloses), RADCAT, AEROCAT, INFOCAT… Encabir-ho dins
d'una integració que es diu "Avisos Meteocat" li posa un sostre artificial: ningú no
buscarà un sensor sísmic dins d'una integració de temps sever.

El CECAT és, estructuralment, **el NINA català**. Es mereix el seu propi domini
(`ha-cecat`, "Protecció Civil Catalunya"), amb `single_config_entry: true` i tots els plans.

### 8.3 Autoritats, llicències i modes de fallada diferents

L'SMC depèn del Departament de Territori; el CECAT, del Departament d'Interior. Les dades
venen de dos serveis sense cap relació tècnica (payload incrustat a `meteo.cat` vs Socrata
a `transparenciacatalunya.cat`). Que un caigui no hauria de tenir cap efecte sobre l'altre
— i amb integracions separades això surt de franc, en lloc d'haver-ho de programar.

### 8.4 El precedent és unànime

`dwd_weather_warnings` + `nina` a HA core (§5), i `DPC-Alert` a Itàlia separat de qualsevol
integració meteorològica (§6). No he trobat cap integració que barregi els dos rols.

### 8.5 Què hi perdem, i per què està bé

L'argument a favor d'ajuntar-les és bo: la cadena **SMC → CECAT → pla** és una sola
història causal, i l'automació interessant és "avís de pluja alt **i** INUNCAT en alerta".
Però això no necessita una integració compartida — necessita **dues entitats a la mateixa
instància de HA**, que és el cas per defecte. Una condició d'automació que creua dos
dominis és exactament igual de fàcil d'escriure.

### 8.6 Conseqüència per a la v1

El CECAT **surt de l'abast de la v1** d'`ha-avisoscat`. La recerca de la font es conserva a
[`01-data-sources.md`](01-data-sources.md) §5 perquè és vàlida i costa de refer, i queda
com a base per a `ha-cecat`, que és una integració petita (un endpoint, sense clau, sense
quota) i independent.

---

## 9. Decisions derivades

| Decisió | Origen |
| --- | --- |
| `SensorDeviceClass.ENUM` amb `options` per al nivell d'avís | `geosphere_austria_warnings` |
| Vigència "ara" recalculada cada cicle amb el rellotge, no només amb el payload | `geosphere_austria_warnings` + franges de 6 h del SMP |
| Un sensor per meteor, a més dels agregats | `figorr/meteocat` (però amb el **grau** com a estat, no `opened`/`closed`) |
| **Separar avís emès (`advance`) d'avís en vigor (`current`)** | `dwd_weather_warnings` — i la naturalesa del SMP, que avisa amb dies d'antelació |
| Events al bus per a automacions | Buit detectat: cap integració catalana en té |
| Sensor de quota només si hi ha API key | `figorr/meteocat` |
| Meteor/llindar desconegut → warning + `unknown` | `figorr/meteocat` |
| Sense dependències de PyPI | `ha-incendiscat` (`requirements: []`) |
| Multi-entrada (N comarques) | `geosphere_austria_warnings` (N municipis) |
| **Protecció Civil en una integració separada** | `nina` vs `dwd_weather_warnings`; `DPC-Alert`; §8 |
