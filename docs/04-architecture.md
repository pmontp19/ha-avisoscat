# Arquitectura tècnica — `ha-avisoscat`

Estructura interna i decisions de disseny. Coherent amb les integracions modernes de HA
(config flow, `DataUpdateCoordinator`, `runtime_data`) i amb les convencions ja establertes
a `ha-incendiscat`.

---

## 1. Layout del repositori

```
ha-avisoscat/
├── custom_components/
│   └── avisoscat/
│       ├── __init__.py
│       ├── manifest.json
│       ├── config_flow.py          # ubicació → comarca, opcions, reauth
│       ├── const.py                # domini, URLs, defaults, claus de config
│       ├── coordinator.py          # AvisoscatDataUpdateCoordinator + events
│       ├── smp.py                  # client dual: font pública / API oficial
│       ├── parser.py               # extracció del payload inline + normalització
│       ├── models.py               # dataclasses i enums, sense imports de HA
│       ├── comarques.py            # taula estàtica id→nom + point-in-polygon
│       ├── vigencia.py             # "és vigent ara?" (franges de 6 h UTC)
│       ├── entity.py               # base CoordinatorEntity + DeviceInfo
│       ├── sensor.py
│       ├── binary_sensor.py
│       ├── diagnostics.py
│       ├── icons.py
│       ├── strings.json
│       ├── quality_scale.yaml
│       ├── translations/{ca,es,en}.json
│       └── brand/icon.png
├── blueprints/automation/avisoscat_warning_notification.yaml
├── tests/
│   ├── fixtures/
│   │   ├── smp_page_sample.html          # pàgina real retallada, payload intacte
│   │   ├── smp_preavisos_sample.json
│   │   ├── smp_temps_violent_sample.json
│   │   └── comarquesAmbMar.json          # TopoJSON real capturat, només per als tests
│   └── test_*.py
├── docs/01-…05-…md                 # aquests documents
├── docs/captures/                  # captures reals; els tests hi llegeixen la base SMP
├── .github/workflows/{ci,validate}.yml
├── hacs.json · pyproject.toml · README.md · CONTRIBUTING.md · LICENSE
```

---

## 2. `manifest.json`

```json
{
  "domain": "avisoscat",
  "name": "Avisos Meteocat",
  "codeowners": ["@pmontp19"],
  "config_flow": true,
  "documentation": "https://github.com/pmontp19/ha-avisoscat",
  "integration_type": "service",
  "iot_class": "cloud_polling",
  "issue_tracker": "https://github.com/pmontp19/ha-avisoscat/issues",
  "quality_scale": "silver",
  "requirements": [],
  "version": "0.1.0"
}
```

- **`requirements: []`** — objectiu explícit, com a `ha-incendiscat`. Només `aiohttp`, que
  ja és a HA core.
- **`integration_type: "service"`** — és un servei al núvol, no un dispositiu.
- **Sense `single_config_entry`**: volem N comarques.

---

## 3. Client (`smp.py`) i parser (`parser.py`)

Dues implementacions darrere d'una única interfície. El coordinator no sap quina fa servir.

```python
class SmpSource(Protocol):
    async def fetch(self) -> SmpSnapshot: ...


class PublicPageSource:  # sense clau — font per defecte
    """Descarrega una pàgina de meteo.cat i n'extreu el payload inline."""


class ApiKeySource:  # api.meteo.cat amb x-api-key
    """Consulta /pronostic/v2/smp/episodis-oberts i /…/preavisos."""
```

El `SmpSnapshot` el defineix el §4. El seu `payload_hash` hi és per saltar-se el
reprocessament quan res no ha canviat: l'equivalent barat del `Last-Modified` que
`geosphere_austria_warnings` sí que té i nosaltres no.

### `PublicPageSource`

1. `GET` amb `Accept-Encoding: gzip` a `SMP_PAGE_URL`
   (`https://www.meteo.cat/observacions/radar`, ~57 KB gzip), amb
   `https://www.meteo.cat/` com a *fallback* si l'extracció no troba episodis.
2. Localitzar `Meteocat.avisosSMP(`.
3. A partir d'aquell offset, per a cada clau (`avisos:`, `episodisPreavisos:`), extreure
   l'array amb un **comptador de claudàtors equilibrat** que ignori els que van dins de
   cadenes. Res de regex greedy: el payload conté `[` i `]` dins de `comentari`.
4. Descartar els arrays buits (l'objecte `opcions` també té una clau `avisos`, buida) i
   quedar-se amb el primer que contingui episodis.
5. `json.loads` → `models.parse_snapshot()`.

Si el pas 2 o el 4 fallen, es llança `SmpParseError` — mai una excepció crua. Tres
fallades seguides disparen `avisoscat_service_degraded` i una *repair issue*.

### `ApiKeySource`

Capçalera `x-api-key`. `403` → `ConfigEntryAuthFailed` (obre reauth). `429` → `UpdateFailed`
amb backoff llarg i **sense reintentar** (cremaria quota). `5xx`/timeout → retry amb
backoff 1s/2s/4s. Consulta `/quotes/v1/consum-actual` un cop al dia per alimentar
`sensor.quota_restant` i ajustar l'interval (§6 de `03-feature-spec.md`).

---

## 4. Models (`models.py`)

Sense cap import de Home Assistant: testable en aïllament total, com `ha-incendiscat`.

```python
class Meteor(StrEnum):
    VENT = "vent"
    PLUJA_30MIN = "pluja_30min"
    PLUJA_3H = "pluja_3h"
    PLUJA_ACUMULADA = "pluja_acumulada"
    NEU = "neu"
    MAR = "mar"
    FRED = "fred"
    CALOR = "calor"
    CALOR_NOCTURNA = "calor_nocturna"
    TEMPS_VIOLENT = "temps_violent"


class TipusAvis(StrEnum):
    PREAVIS = "preavis"
    AVIS = "avis"
    VIGILANCIA = "vigilancia"
    TEMPS_VIOLENT = "temps_violent"


class NivellPerill(StrEnum):
    """Codi semafòric oficial (grau 0-6 → 4 categories)."""

    CAP = "cap"  # 0
    MODERAT = "moderat"  # 1-2
    ALT = "alt"  # 3-4
    MOLT_ALT = "molt_alt"  # 5-6

    @classmethod
    def from_perill(cls, perill: Any) -> NivellPerill: ...


@dataclass(frozen=True, slots=True)
class Afectacio:
    id_comarca: int
    perill: int  # 0-6
    nivell: int  # 1 = llindar baix, 2 = llindar alt
    llindar: str
    auxiliar: bool
    dia: date | None


@dataclass(frozen=True, slots=True)
class Evolucio:
    dia: date | None
    comentari: str
    llindar_baix: str | None
    llindar_alt: str | None
    distribucio_geografica: str | None
    representatiu: int | None
    periodes: dict[str, tuple[Afectacio, ...]]  # "00-06" … "18-00"


@dataclass(frozen=True, slots=True)
class Avis:
    tipus: TipusAvis | None  # None si el literal no es reconeix (trap 9)
    tipus_nom: str  # literal cru del Meteocat, sempre preservat
    estat: str
    data_emissio: datetime | None
    data_inici: datetime | None
    data_fi: datetime | None
    evolucions: tuple[Evolucio, ...]


@dataclass(frozen=True, slots=True)
class Episodi:
    meteor: Meteor | None  # None si el nom no es reconeix
    meteor_nom: str  # nom cru del Meteocat, sempre preservat
    estat: str
    avisos: tuple[Avis, ...]


@dataclass(frozen=True, slots=True)
class Preavis:
    """Forma pròpia: sense comarca ni franges (§6 de `01-data-sources.md`)."""

    tipus: TipusAvis | None
    tipus_nom: str
    estat: str
    perill: int
    nivell: int
    llindar: str
    comentari: str
    data_emissio: datetime | None
    data_inici: datetime | None
    data_fi: datetime | None
    meteor: Meteor | None = None  # només l'endpoint amb clau l'envia
    meteor_nom: str = ""


@dataclass(frozen=True, slots=True)
class SmpSnapshot:
    episodis: tuple[Episodi, ...] = ()
    preavisos: tuple[Preavis, ...] = ()
    fetched_at: datetime | None = None
    payload_hash: str | None = None
```

Tres decisions del model que no es llegeixen del sketch:

- Els `date | None` i el `TipusAvis | None` són deliberats: un dia o un literal de
  tipus il·legible no ha de descartar l'afectació ni l'avís sencers. El literal cru
  sempre queda a `tipus_nom`, igual que `meteor_nom`.
- Les col·leccions són tuples perquè els dataclasses congelats comparin per valor. És
  el que permetrà al coordinator comparar snapshots amb `always_update=False` (§7).
- `parse_snapshot()` **no llança mai**: entrada malformada → snapshot buit i un
  warning. Cada entrada (episodi, avís, evolució, franja, afectació) se salta
  individualment, de manera que un membre malformat no descarti els veïns sans.

`is_closed(estat)` és pública precisament perquè `vigencia.py` (§5) necessita el
mateix test de tancament.

### Els 11 traps de tolerància, codificats

Cadascun dels traps de [`01-data-sources.md`](01-data-sources.md) §6 té un helper i un test:

| Trap | Implementació |
| --- | --- |
| Floats (`2.0`) | `_as_int(value, default)` amb `int(float(...))` dins d'un `try` que absorbeix també `OverflowError`: `Infinity` i `1e999` són JSON vàlid |
| `afectacions: null` | `_as_list(value, camp)`: el `null` documentat es llegeix com a cap entrada en silenci, qualsevol altre tipus com a cap entrada **amb** warning |
| `estat: "Ampliat"` | **No es filtra per literal.** `is_closed(estat)` només reconeix estats de tancament coneguts; qualsevol altra cosa es considera oberta i la vigència la decideix `vigencia.py` |
| Múltiples `avisos` per episodi | `_dedupe_avisos()`: agrupa per `(meteor, tipus)`, guanya el `data_emissio` més recent; empat → grau més alt |
| Meteor desconegut | `_parse_meteor()` case-insensitive amb *prefix match*; retorna `None` + `_LOGGER.warning`, i `meteor_nom` conserva el text cru |
| Tipus amb variants històriques | `_parse_tipus()` per `casefold()` + prefix (`"avís vigilància per temps violent"` abans que `"avís vigilància"`, abans que `"avís"`) |
| `idMeteor: null` | Mai s'usa com a clau |
| Franja `"18-00"` | La clau del `dict` ve del JSON, no d'una constant nostra |
| `idComarca` desconegut | `comarques.nom(id)` → `f"Comarca {id}"` amb warning |
| Text extern | El model el desa verbatim, mai escapat ni reformat; mai HTML a l'altra banda (§11) i el README avisa |
| Parseig explícit de dates | `_parse_datetime()` / `_parse_date()`: `None` + warning si el timestamp no es pot llegir, i naïf assumit UTC |

---

## 5. Vigència (`vigencia.py`)

El mòdul més important i el que ens diferencia. Un avís SMP **no** és vigent només perquè
existeixi: cal creuar `dataInici`/`dataFi` amb la franja de 6 h UTC del moment actual. I en
surten **dos horitzons, no un** (§1.1 de [`03-feature-spec.md`](03-feature-spec.md)): dues
projeccions del mateix snapshot separades només pel rellotge.

```python
PERIODES = {"00-06": (0, 6), "06-12": (6, 12), "12-18": (12, 18), "18-00": (18, 24)}
FINESTRA_TEMPS_VIOLENT = timedelta(hours=2)
DIES_OUTLOOK = 3


class Horitzo(StrEnum):
    VIGENT = "vigent"  # la franja afectada conté aquest instant
    ANUNCIAT = "anunciat"  # emès, la franja encara no ha començat
    PASSAT = "passat"  # la franja ja ha acabat


@dataclass(frozen=True, slots=True)
class AfectacioProjectada:
    """Una afectació d'una comarca situada sobre el rellotge."""

    horitzo: Horitzo
    id_comarca: int
    meteor: Meteor | None
    meteor_nom: str
    tipus: TipusAvis | None
    tipus_nom: str
    perill: int  # 0-6
    nivell: int  # 1 = llindar baix, 2 = llindar alt
    llindar: str
    auxiliar: bool
    dia: date
    periode: str  # nom canònic de franja
    inici: datetime  # inici de franja retallat per `dataInici`
    fi: datetime  # fi de franja retallat per `dataFi`, exclusiu
    dies_per_endavant: int
    hores_per_endavant: int
    comentari: str
    distribucio_geografica: str | None
    data_emissio: datetime | None
    data_inici: datetime | None
    data_fi: datetime | None


def periode_actual(now_utc: datetime) -> str: ...
def periode_bounds(dia: date, periode: str) -> tuple[datetime, datetime] | None: ...


# El recorregut únic del qual es filtren els dos horitzons i la graella.
def projeccions(
    episodis: Iterable[Episodi], id_comarca: int, now_utc: datetime
) -> list[AfectacioProjectada]: ...


def afectacions_vigents(...) -> list[AfectacioProjectada]:
    """Afectacions que apliquen a `id_comarca` en aquest instant."""


def afectacions_anunciades(...) -> list[AfectacioProjectada]:
    """Afectacions emeses per a `id_comarca` que encara no són vigents."""


def outlook(..., *, dies: int = DIES_OUTLOOK) -> list[OutlookDia]:
    """Graella dia × franja: sempre 4 franges per dia, amb 0 on no hi ha res."""


def preavisos_actius(
    preavisos: Iterable[Preavis], now_utc: datetime
) -> list[Preavis]: ...
```

Els **Avisos de Vigilància per Temps Violent** són un cas a part, no una franja doblegada:
valen 2 h des de `dataEmisio`, ignoren la franja on el payload els llista i **mai** són
anunciats (quan existeixen ja són vigents; una emissió amb data futura no informa res). Les
franges on apareixen es col·lapsen en una sola projecció, la més greu, perquè un nowcast
repetit a dues franges no compti com dos avisos vigents.

Vuit decisions que no es llegeixen del sketch:

- **Un sol `AfectacioProjectada`** en lloc d'un `AfectacioVigent`: una afectació vigent i
  una anunciada porten exactament la mateixa càrrega i només difereixen del costat del
  rellotge on cauen. Una classe que es digués "vigent" i contingués una afectació anunciada
  mentiria; el camp `horitzo` ho diu sense ambigüitat.
- **`Horitzo.PASSAT`** existeix perquè `outlook()` pugui informar del grau d'una franja
  d'avui que ja ha passat. Cap dels dos horitzons de cap el retorna mai.
- **L'interval efectiu és la franja retallada** per `dataInici`/`dataFi`: un avís que acaba
  a mitja franja deixa de ser vigent al seu `dataFi`, no al final de la franja. Si el retall
  queda buit, la franja simplement no s'informa (la font continua enviant-la).
- **`fi` és exclusiu**: la franja `18-00` va de les 18:00 a la mitjanit següent, i és per
  això que les 23:59:59 encara compten.
- **Tot en UTC**, i `now_utc` es normalitza a l'entrada (naïf → UTC, *aware* → convertit),
  perquè l'hora oficial (UTC+1 a l'hivern, UTC+2 a l'estiu) no es pugui filtrar dins
  l'aritmètica de franges. Hi ha un test d'hivern i un d'estiu que ho demostren.
- **Els noms de franja es parsegen, no es busquen**: el `"18-24"` de la documentació escrita
  de l'SMC resol igual que el `"18-00"` del JSON i tots dos es normalitzen a `"18-00"`. Un
  nom que no es pugui situar en el temps es descarta amb warning, mai s'endevina.
- **No es filtra per grau**: un grau il·legible també val 0 (§4), així que descartar el 0
  aquí perdria afectacions reals. El llindar de severitat és cosa de les entitats.
- **`outlook()` reparteix per solapament d'interval**, no per nom de franja: així una
  finestra de temps violent a cavall de les 18:00 apareix a les dues cel·les. I
  `preavisos_actius()` no es parteix en dos horitzons: un preavís no té comarca ni franja i
  el seu sentit és justament l'horitzó de 3 dies i més (§1.5 de
  [`01-data-sources.md`](01-data-sources.md)).

⚠️ Com que la vigència depèn del rellotge, el coordinator **no** pot limitar-se a recalcular
quan arriba dada nova. `__init__.py` registra un `async_track_time_change` cada minut que
força `coordinator.async_set_updated_data(recompute())` sense fer cap petició de xarxa.
Sense això, un avís que comença a les 12:00 UTC no s'encendria fins al següent poll.

---

## 6. Comarques (`comarques.py`)

Taula estàtica de 55 entrades (43 terrestres + 12 marítimes) **generada un cop** a partir de
`comarquesAmbMar.json` i incrustada al codi: id, nom, i la zona marítima adjacent quan
n'hi ha. Zero peticions en temps d'execució i zero dependències.

La geometria per a *point-in-polygon* **només** cal al config flow. Per no incrustar 58 KB
de TopoJSON al component distribuït ni afegir `shapely`:

1. El config flow descarrega `comarquesAmbMar.json` una vegada, en el moment de resoldre la
   ubicació.
2. Decodifica el TopoJSON (aritmètica d'arcs, ~40 línies) i fa ray casting pur.
3. `async_resolve_comarca()` **no llança mai**: retorna un `ComarcaResolution` amb
   `id_comarca`, o amb `error` (`cannot_connect`, `invalid_geometry`,
   `location_outside_catalonia`, que són alhora les claus d'error del config flow). Si la
   descàrrega o la descodificació falla, el flux cau al desplegable manual de comarques i
   mai queda bloquejat.

Un `id` que no és a la taula retorna `f"Comarca {id}"` (Moianès i Lluçanès són recents; en
poden aparèixer més).

La captura real del TopoJSON sí que és al repositori, a
`tests/fixtures/comarquesAmbMar.json`: és dada de test (`CONTRIBUTING.md` exigeix fixtures
reals i zero xarxa), no geometria distribuïda amb el component.

---

## 7. Coordinator

Un de sol. Els plans de Protecció Civil van a una integració separada
([`02-existing-integrations.md`](02-existing-integrations.md) §8), així que aquí no hi ha
res a compondre:

```python
type AvisoscatConfigEntry = ConfigEntry[AvisoscatDataUpdateCoordinator]
```

> `ha-incendiscat` va acabar penjant el seu segon coordinator com a atribut del primer per
> minimitzar el diff (vegeu el comentari a `custom_components/incendiscat/__init__.py`).
> Aquí el problema no existeix: una font, un coordinator. Si algun dia en calgués un segon,
> es faria amb un contenidor tipat, no amb un atribut.

Estat que manté:

```python
@dataclass
class AvisoscatState:
    snapshot: SmpSnapshot | None
    en_vigor: dict[Meteor, AfectacioVigent]  # actiu ARA
    anunciats: dict[Meteor, AfectacioFutura]  # emès, encara no vigent
    outlook: dict[date, dict[str, int]]  # dia -> {franja: grau}, 3 dies
    preavis: Preavis | None
    temps_violent: TempsViolent | None
    last_success: datetime | None
    last_error: str | None
    consecutive_failures: int
    quota: QuotaInfo | None
    announced_seen: set[tuple[Meteor, TipusAvis, datetime]]  # dedup d'anuncis
```

`en_vigor` i `anunciats` són **dues projeccions del mateix snapshot**, separades pel
rellotge (§1.1 de `03-feature-spec.md`). `outlook` alimenta els sensors `grau_maxim_*` i
les seves graelles per franja.

Cicle:

1. `source.fetch()`; si falla → conservar `snapshot`, incrementar `consecutive_failures`,
   marcar `last_error`. **Mai esborrar dades bones.**
2. Recalcular `en_vigor`, `anunciats` i `outlook` amb `vigencia`.
3. Comparar amb el cicle anterior → emetre events (§8).
4. `always_update=False` (`AvisoscatState` implementa `__eq__`) per estalviar escriptures
   d'estat.

Els passos 2–3 també els executa el tick d'un minut de `__init__.py`, **sense el pas 1**:
és el que converteix un canvi de franja en `avisoscat_warning_started` sense tocar la
xarxa.

## 8. Detecció d'events

Dos bucles independents, un per horitzó.

```python
def _emit_announced(state, anunciats) -> None:
    """Emissions noves. Dedup per (meteor, tipus, data_emissio)."""
    for meteor, af in anunciats.items():
        key = (meteor, af.tipus, af.data_emissio)
        if key not in state.announced_seen:
            state.announced_seen.add(key)
            fire(EVENT_WARNING_ANNOUNCED, payload_announced(af))


def _emit_in_force(prev: dict[Meteor, AfectacioVigent], curr) -> None:
    for meteor, af in curr.items():
        old = prev.get(meteor)
        if old is None:
            fire(EVENT_WARNING_STARTED, payload_started(af))
        elif af.perill > old.perill:
            fire(EVENT_WARNING_UPGRADED, payload(af, old))
        elif af.perill < old.perill:
            fire(EVENT_WARNING_DOWNGRADED, payload(af, old))
    for meteor, old in prev.items():
        if meteor not in curr:
            fire(EVENT_WARNING_CLEARED, payload_cleared(old))
```

Detalls que importen:

- **`announced_seen` es purga** quan l'avís entra en vigor o expira; si no, creix sense
  límit. Viu en memòria: després d'un reinici de HA es reconstrueix marcant com a ja vistos
  tots els anuncis del snapshot inicial, de manera que **arrencar no dispara una allau
  d'events** d'avisos que ja fa dies que estan emesos.
- **Una ampliació sí que renotifica**: `estat: "Ampliat"` porta un `dataEmisio` nou, que és
  una clau nova. És el comportament desitjat — l'SMC ha canviat alguna cosa.
- `avisoscat_violent_weather` es dispara un cop per `dataEmisio`, no a cada cicle mentre
  dura la finestra de 2 h.
- `avisoscat_service_degraded` es dispara **una sola vegada** en creuar
  `DEGRADED_FAILURE_THRESHOLD = 3`, no a cada cicle.

## 9. Entitats

```python
PLATFORMS = (Platform.BINARY_SENSOR, Platform.SENSOR)
```

Base comuna a `entity.py`:

```python
class AvisoscatEntity(CoordinatorEntity[AvisoscatDataUpdateCoordinator]):
    _attr_has_entity_name = True
    _attr_attribution = "Dades del Servei Meteorològic de Catalunya (Meteocat)"

    def __init__(self, coordinator, entry, key):
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=f"Avisos Meteocat — {coordinator.comarca_nom}",
            manufacturer="Servei Meteorològic de Catalunya",
            model="Situació Meteorològica de Perill",
            entry_type=DeviceEntryType.SERVICE,
            configuration_url="https://www.meteo.cat/",
        )
```

Els sensors de nivell fan servir `SensorDeviceClass.ENUM` amb
`options=["cap","moderat","alt","molt_alt"]` (patró de `geosphere_austria_warnings`), de
manera que les targetes i les condicions d'automació els tracten com a estats discrets.

Icones a `icons.py`: `mdi:weather-windy` (vent), `mdi:weather-pouring` (pluja),
`mdi:weather-snowy-heavy` (neu), `mdi:waves` (mar), `mdi:snowflake-alert` (fred),
`mdi:thermometer-alert` (calor), `mdi:weather-lightning` (temps violent),
`mdi:shield-alert` (Protecció Civil).

**Traduccions**: cada entitat i cada camp del config flow necessita clau als **tres**
fitxers `translations/{ca,es,en}.json` o `hassfest` falla. Català com a llengua de
referència.

---

## 10. Resiliència

| Fallada | Comportament |
| --- | --- |
| Timeout / xarxa | 3 retries amb backoff 1s/2s/4s → `service_connected = false`, es conserva l'estat cachejat |
| Pàgina pública canvia de marcatge (`SmpParseError`) | Es prova el *fallback* `https://www.meteo.cat/`; si també falla, es conserva l'estat i s'incrementa el comptador |
| HTTP 403 amb API key | `ConfigEntryAuthFailed` → flux de reauth |
| HTTP 429 | `UpdateFailed` + interval duplicat temporalment. **Cap retry** (cremaria quota) |
| HTTP 5xx | Retry amb backoff |
| JSON invàlid | Log + es conserva la cache |
| Camp esperat absent | `.get()` amb default a `models.py`. Warning, mai excepció |
| 3 fallades seguides del mateix tipus | `avisoscat_service_degraded` + *repair issue* amb `learn_more_url` als issues del repo |

### Límits de sondeig

- **Adaptatiu** amb la font pública: 30 min sense cap episodi obert, 10 min quan n'hi ha
  algun (§6 de `03-feature-spec.md`). El 10 min només es justifica pel nowcast de temps
  violent, que només apareix en situacions convectives.
- Mínim absolut **10 minuts**: `cache-control: max-age=600`. Sondejar més sovint és
  consumir amplada de banda d'un servei públic per a res.
- Amb API key, l'interval el marca la quota, mai l'usuari sol.
- El recàlcul de vigència per canvi de franja és **local**: no genera cap petició. És el
  que permet que el sondeig sigui lent sense que els events arribin tard.

---

## 11. Seguretat i dades

- `comentari`, `llindar` i `meteor_nom` són **text extern no fiable**:
  mai `allow_html`, mai interpolació HTML directa. El README ho ha de dir per a qui faci
  targetes Markdown.
- `diagnostics.py` redacta `latitude`, `longitude` i `api_key` abans d'exportar.
- L'API key es desa a `entry.data`, mai a `entry.options`, i no apareix mai als logs.
- La ubicació de l'usuari **no s'envia enlloc**: el point-in-polygon és local i, un cop
  resolta la comarca, només es desa l'`id_comarca`.

---

## 12. Tests

`pytest-homeassistant-custom-component` + `aioresponses`, **zero xarxa real**. Lògica
dependent del rellotge amb un `FakeClock` (fixture `clock`), mai `sleep()` ni `freezegun` —
i aquí és especialment crític, perquè tota la vigència depèn de l'hora UTC.

Cobertura mínima **95%** (`--cov-fail-under=95`, igual que CI).

Fixtures **reals capturades**, mai inventades. La captura base ja existeix: payload del
2026-08-05 amb dos episodis d'intensitat de pluja en estat `Ampliat`, franges buides
(`afectacions: null`) i plenes, i floats a `perill`/`idComarca`/`nivell` — cobreix 5 dels
11 traps ella sola.

Casos obligatoris:

- `test_parser.py`: extracció del payload de la pàgina real, claudàtors dins de cadenes,
  `avisos` buit d'`opcions` ignorat, pàgina sense episodis.
- `test_models.py`: un test per cada trap de `01-data-sources.md` §6.
- `test_vigencia.py`: canvi de franja a 12:00 UTC, avís que acaba a mitja franja, temps
  violent i la seva finestra de 2 h, horari d'estiu vs hivern.
- `test_coordinator.py`: anunci/inici/pujada/baixada/resolució i els seus events; un
  anunci no es repeteix, una ampliació (`dataEmisio` nou) sí, i arrencar amb avisos ja
  emesos no dispara cap `announced`.
- `test_resilience.py`: parse error → fallback → degraded a la tercera; 403 → reauth;
  429 → sense retry.
- `test_translations.py`: totes les claus presents als tres idiomes.

---

## 13. CI/CD

Idèntic a `ha-incendiscat`: `ci.yml` (`ruff check .`, `ruff format --check .`,
`pytest --cov=custom_components/avisoscat --cov-fail-under=95`) i `validate.yml` (hassfest
+ validació HACS). Release amb `release-please` i Conventional Commits.

---

## 14. Decisions arquitecturals

| Decisió | Per què |
| --- | --- |
| Repositori separat, domini `avisoscat` | HACS distribueix una integració per repo. Barrejar-ho amb `incendiscat` trencaria el packaging i el `release-please` d'aquell repo |
| Font pública per defecte, API key opcional | La quota ciutadana (~100/mes) fa impossible el temps real. La font pública dona 10 min i zero fricció d'instal·lació |
| Client dual darrere d'un `Protocol` | El coordinator, els models i les entitats no saben d'on venen les dades; canviar de font és canviar una línia |
| Grau de perill com a **estat** (`ENUM`), no com a atribut | És el que fa que les automacions siguin trivials. `figorr/meteocat` posa `opened`/`closed` a l'estat i obliga a llegir atributs |
| Recàlcul de vigència per rellotge cada minut | Les franges de 6 h canvien sense que canviï la font. Sense això, els avisos arriben tard |
| **Separar `announced` de `started`** | El SMP avisa amb dies d'antelació i només el temps violent és nowcast. Un sol event obligaria a triar entre notificar massa aviat o massa tard. Precedent: `advance`/`current` del `dwd_weather_warnings` de HA core |
| **Sondeig adaptatiu 30/10 min** | 10 min només cal per al nowcast convectiu; la resta de l'any seria triplicar la càrrega sobre un servei públic per res |
| Events al bus a més de binary sensors | Patró event-driven de HA; "acaba d'entrar un avís de vent" és un event, no un estat |
| Multi-entrada (N comarques) | Casa, feina, família. `geosphere_austria_warnings` fa el mateix amb municipis |
| Taula de comarques incrustada, geometria només al config flow | Zero peticions en runtime, zero dependències, 58 KB fora del component (§6) |
| **CECAT en una integració separada (`ha-cecat`)** | Àmbit territorial incompatible (Catalunya sencera vs comarca: N entrades donarien N còpies del mateix INUNCAT), abast natural molt més gran (SISMICAT, TRANSCAT…) i precedent `nina` / `dwd_weather_warnings` a HA core. Detall a `02-existing-integrations.md` §8 |
| `requirements: []` | Menys superfície de trencament amb els canvis de HA. Mateix criteri que `ha-incendiscat` |
| Codi en anglès, UI en català | Convenció HA + context d'ús |
