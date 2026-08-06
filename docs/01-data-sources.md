# Fonts de dades — Avisos de temps sever del Meteocat (SMP)

Resultat de la recerca feta el 2026-08-05 sobre el sistema d'avisos de **Situació
Meteorològica de Perill (SMP)** del Servei Meteorològic de Catalunya (SMC), l'API oficial
`api.meteo.cat`, les fonts públiques sense clau i les dades obertes de Protecció Civil.

La recerca es va fer al repositori d'[`ha-incendiscat`](https://github.com/pmontp19/ha-incendiscat),
amb qui aquesta integració comparteix convencions d'enginyeria, i es va traslladar aquí en
crear el repositori propi.

---

## 1. Teoria: què és una SMP

L'SMC identifica una **Situació Meteorològica de Perill** quan preveu la superació d'uns
llindars específics per a cada **meteor** (fenomen). El comunicat s'envia immediatament al
**CECAT** (Centre de Coordinació Operativa de Catalunya), que activa el pla de Protecció
Civil corresponent.

Aquesta cadena **SMC → CECAT → pla** és una sola història causal però són **dues
autoritats**, amb cadències i abasts diferents. El §5 documenta la font del CECAT, però
va a una integració separada (vegeu-hi la nota).

Referències oficials:

- <https://www.meteo.cat/wpweb/divulgacio/la-prediccio-meteorologica/situacio-meteorologica-de-perill/>
- <https://www.meteo.cat/wpweb/divulgacio/la-prediccio-meteorologica/situacio-meteorologica-de-perill/distribucio-territorial-de-llindars/>

### 1.1 Unitat territorial: la comarca

**Els avisos SMP es resolen per comarca, mai per municipi.** La ubicació de l'usuari s'ha
de resoldre a comarca (§4). El territori es divideix en **43 comarques terrestres** i **12
zones marítimes** (§4.2).

> Els avisos de temps violent s'agrupen també en comarques "si bé el Temps Violent sovint
> afectarà només un o alguns municipis, ja que es tracta de fenòmens meteorològics molt
> locals" — text literal de l'SMC. Cal reflectir-ho a la documentació d'usuari: un avís
> actiu a la teva comarca **no** vol dir que et caigui a sobre.

### 1.2 Franges horàries

Cada dia d'avís es divideix en **4 franges de 6 hores en UTC**:

| Franja | UTC | Hora oficial (hivern) | Hora oficial (estiu) |
| --- | --- | --- | --- |
| `00-06` | 00:00–06:00 | 01:00–07:00 | 02:00–08:00 |
| `06-12` | 06:00–12:00 | 07:00–13:00 | 08:00–14:00 |
| `12-18` | 12:00–18:00 | 13:00–19:00 | 14:00–20:00 |
| `18-00` | 18:00–24:00 | 19:00–01:00 | 20:00–02:00 |

⚠️ La documentació escrita de l'SMC anomena la darrera franja "18-24", però **el JSON real
la retorna com a `"18-00"`**. Fer servir el valor del JSON.

### 1.3 Llindars per meteor

**Temps violent** (llindar únic, sense escala baixa/alta):

- Pedra de diàmetre > 2 cm
- Ratxes de vent > 90 km/h (25 m/s)
- Esclafits
- Tornados o mànegues

**Meteors amb llindar baix / llindar alt:**

| Meteor | Llindar baix | Llindar alt |
| --- | --- | --- |
| Intensitat de pluja en 30 minuts | > 20 mm / 30 min | > 40 mm / 30 min |
| Intensitat de pluja en 3 hores | > 60 mm / 3 h | > 90 mm / 3 h |
| Acumulació de pluja | > 100 mm / 24 h | > 200 mm / 24 h |
| Estat de la mar | Onades > 2,50 m (maregassa) | Onades > 4,00 m (mar brava) |
| Fred | Fred intens (percentil 2 de la T mín. diària de desembre a març) | Fred molt intens (percentil 2 − 2 °C) |
| Calor | Calor intensa (percentil 98 de la T màx. diària de juny a agost) | Calor molt intensa (percentil 98 + 2 °C) |
| Calor nocturna (**avís en proves**) | Calor nocturna intensa (percentil 98 de la T mín.) | Calor nocturna molt intensa (percentil 98 + 2 °C) |

**Neu acumulada en 24 h** — el llindar depèn de la cota:

| Cota | Llindar baix | Llindar alt |
| --- | --- | --- |
| < 300 m | gruix ≥ 0 cm | gruix > 5 cm |
| 300–600 m | gruix > 2 cm | gruix > 15 cm |
| 600–800 m | gruix > 5 cm | gruix > 20 cm |
| 800–1000 m | gruix > 10 cm | gruix > 30 cm |
| 1000–1500 m | gruix > 20 cm | gruix > 50 cm |

**Vent** — el llindar varia **per comarca** (definit al Pla VENTCAT d'Interior):

| Llindar baix / alt | Comarques |
| --- | --- |
| > 72 km/h (20 m/s) / > 108 km/h (30 m/s) | Anoia, Alt Penedès, Bages, Baix Llobregat, Baix Penedès, Barcelonès, Garraf, Gironès, Lluçanès, Maresme, Moianès, Osona, Pla d'Urgell, Segarra, Segrià, Selva, Tarragonès, Urgell, Vallès Occidental, Vallès Oriental |
| > 90 km/h (25 m/s) / > 126 km/h (35 m/s) | Alt Camp, Alt Urgell, Alta Ribagorça, Baix Camp, Baix Empordà, Berguedà, Cerdanya, Conca de Barberà, Garrigues, Garrotxa, Noguera, Pallars Jussà, Pallars Sobirà, Pla de l'Estany, Priorat, Ribera d'Ebre, Ripollès, Solsonès, Terra Alta, Val d'Aran |
| > 108 km/h (30 m/s) / > 144 km/h (40 m/s) | Alt Empordà, Baix Ebre, Montsià |

Els llindars de fred, calor i calor nocturna són **per municipi** (interpolació de la XEMA
i la XOM sobre els últims 15 anys) i l'SMC en publica taules PDF/CSV. **No cal
implementar-los**: l'API ja retorna el llindar aplicat com a text (`llindar1`/`llindar2`).

### 1.4 Grau de perill (0–6) i codi semafòric

El grau surt del creuament entre el llindar que es pot superar i la probabilitat
d'ocurrència:

| | Probabilitat baixa (10–30%) | Probabilitat mitjana (30–70%) | Probabilitat alta (>70%) |
| --- | :---: | :---: | :---: |
| **Llindar alt** | 4 | 5 | 6 |
| **Llindar baix** | 1 | 2 | 3 |
| **Sense avís** | 0 | 0 | 0 |

Agrupació en 4 colors. **Verificat al JS oficial de `meteo.cat`** (`_crearAvisosCombinatsLayer`,
`switch(perillMax)`), no inferit:

| Grau | Categoria | Color oficial |
| :---: | --- | --- |
| 0 | Sense perill | `#B4C828` (verd) |
| 1–2 | **Moderat** | `#fff200` (groc) |
| 3–4 | **Alt** | `#e99b15` (taronja) |
| 5–6 | **Molt alt** | `#cf0920` (vermell) |

Els **Avisos de Vigilància** fan servir una taula diferent (grau 3-4 = comarques afectades
a curt termini, 5-6 = comarques afectades).

### 1.5 Tipologies d'avís

Literals exactes, extrets de les constants del JS oficial (`script.min.js`):

```js
AVIS_ESTAT_VIGENT   = "Vigent"
TIPUS_AVIS          = "Avís"
TIPUS_PREAVIS       = "Preavís"
TIPUS_OBSERVACIO    = "Avís Vigilància"
TIPUS_TEMPS_VIOLENT = "Avís Vigilància per Temps Violent"
```

| Tipus | Horitzó | Granularitat | Notes |
| --- | --- | --- | --- |
| **Preavís** | A partir del 3r dia de predicció | **Catalunya sencera** (sense comarca) | Grau màxim de l'episodi + descripció de l'evolució |
| **Avís** | Dia present fins al 3r dia | Comarca × franja de 6 h | El cas principal. Inclou distribució geogràfica |
| **Avís Vigilància** | Poques hores vista | Comarca | Superació detectada en una zona no avisada prèviament, o èmfasi en una zona ja avisada |
| **Avís Vigilància per Temps Violent** | **Vigència de 2 h** | Comarca | Basat en el *lightning jump* observat per la XDDE. Probabilitat alta (>70%) o mitjana (30–70%) |

⚠️ El codi antic del web també conté les variants `"Avís d'Observació"` i
`"Avís temps violent"`. **Cap comparació amb aquests literals pot ser exhaustiva** — vegeu
§6.

### 1.5.1 Antelació real — el SMP no és un sistema de temps real

| Tipus | Antelació típica | Naturalesa |
| --- | --- | --- |
| Preavís | 3 dies o més | Planificació |
| **Avís** | Del dia present al 3r dia | **Predicció** — el gruix del sistema |
| Avís Vigilància | Hores | Curt termini |
| **Avís Vigilància per Temps Violent** | **Minuts** (2 h de vigència) | **Nowcast** |

Això té conseqüències directes sobre el sondeig i el model d'entitats: només l'última fila
justifica sondejar cada 10 minuts, i el gruix del valor està a saber **què ve**, no només
què hi ha ara. Vegeu `03-feature-spec.md` §1.1 i §6.

### 1.6 Distribució geogràfica

Camp `distribucioGeografica` de cada evolució, indica quina part de la zona avisada
s'espera afectada:

| Valor | Significat |
| --- | --- |
| `LOCAL` | < 30% de la zona avisada |
| `EXTENSA` | 30–70% |
| `GENERAL` | > 70% |

---

## 2. API oficial `api.meteo.cat` — sancionada però insuficient per a temps real

Documentació: <https://apidocs.meteocat.gencat.cat/documentacio/>

### 2.1 Endpoints rellevants

| Recurs | Mètode i URL | Versió |
| --- | --- | --- |
| Episodis oberts i avisos vigents d'un dia | `GET https://api.meteo.cat/pronostic/v2/smp/episodis-oberts?data={any}-{mes}-{dia}Z` | 2.0 |
| Episodis oberts del dia present | `GET https://api.meteo.cat/pronostic/v1/smp/episodis-oberts` | 1.0 |
| Preavisos publicats actualment | `GET https://api.meteo.cat/pronostic/v1/smp/episodis-oberts/preavisos` | 1.0 |
| Referència de comarques | `GET https://api.meteo.cat/referencia/v1/comarques` | 1.0 |
| Referència de municipis | `GET https://api.meteo.cat/referencia/v1/municipis` | 1.0 |
| Consum de quota | `GET https://api.meteo.cat/quotes/v1/consum-actual` | 1.0 |

Exemple real: `https://api.meteo.cat/pronostic/v2/smp/episodis-oberts?data=2023-06-13Z`

`/referencia/v1/municipis` retorna codi INE de 6 dígits, nom, coordenades i comarca —
exactament el que caldria per resoldre ubicació → comarca, però requereix clau:

```json
{ "codi": "250019", "nom": "Abella de la Conca",
  "coordenades": { "latitud": 42.16239244076299, "longitud": 1.0928929183862726 },
  "comarca": { "codi": 25, "nom": "Pallars Jussà" } }
```

### 2.2 Autenticació

Capçalera HTTP **`x-api-key`** amb el codi alfanumèric del client. **CORS deshabilitat** a
propòsit: l'SMC exigeix que les consultes es facin des de servidor, no des del navegador —
Home Assistant hi encaixa.

Verificat el 2026-08-05: sense clau, `GET /pronostic/v2/smp/episodis-oberts` retorna
`403 {"message":"Forbidden"}`.

### 2.3 Codis d'error

Cos JSON amb `message` (text) i opcionalment `aws` (per a suport).

| Codi | Causa |
| :---: | --- |
| 400 | Paràmetres incorrectes; el cos indica la causa exacta |
| 403 | `{"message":"Forbidden"}` → sense permís. `{"message":"Missing Authentication Token"}` → el recurs no existeix |
| **429** | **Quota periòdica superada**, o més de 1000 peticions/segon |
| 500 | Error intern (p. ex. dades no disponibles) |

### 2.4 Quota ⚠️ el constrenyiment que ho decideix tot

- Les quotes són **mensuals** i es reseteixen l'1 de cada mes a les **00:00 UTC**.
- Cal subscriure's per separat a cada pla. Els avisos SMP són al pla **"Dades de
  Predicció"**.
- L'accés ciutadà és gratuït però es demana per **formulari** (<https://apidocs.meteocat.gencat.cat/documentacio/acces-ciutada-i-administracio/>),
  amb resposta en ~7 dies i **durada limitada** (1 mes / 3 mesos / 6 mesos / 1 any).
- `GET /quotes/v1/consum-actual` retorna el consum en viu:

```json
{ "client": { "nom": "Client1", "apiKey": "xx...xxx" },
  "plans": [ { "nom": "Quotes" },
             { "nom": "Referencia Bàsic", "periode": "Mensual",
               "maxConsultes": 1000, "consultesRestants": 982, "consultesRealitzades": 18 } ] }
```

**L'SMC no publica el valor de la quota ciutadana.** L'evidència indirecta (defaults del
config flow de `figorr/meteocat`, l'única integració HA que la consumeix) apunta a:

| Pla | Peticions/mes assumides |
| --- | ---: |
| Dades de la XEMA | 750 |
| **Dades de Predicció** | **100** |
| Dades de la XDDE | 250 |
| Quotes | 300 |
| Referència Bàsic | 2000 |

**~100 peticions/mes són ~3/dia.** `figorr/meteocat` ho reconeix multiplicant per 12 el
seu interval base de 120 min quan `limit_prediccio <= 100`, és a dir **refrescant els
avisos un cop cada 24 h**. Amb API key sola no hi ha temps real possible.

L'SMC recomana explícitament implementar **caché a servidor** i no fer una petició per
cada consulta d'usuari final.

---

## 3. Font pública sense clau ⭐ (font principal)

`meteo.cat` **incrusta el payload SMP sencer** dins de les seves pàgines, en una crida JS
del seu propi bundle:

```js
Meteocat.avisosSMP({
    dom: 'mapaAvisos',
    domLlistat: 'llistatAvisos',
    data: '2026-08-05Z',
    prediccions: [ ... ],
    avisos: [[ /* episodis oberts — MATEIX esquema que /pronostic/v2/smp/episodis-oberts */ ]],
    episodisPreavisos: [ ... ]
});
```

**És el mateix esquema JSON exacte que l'endpoint v2 de l'API**, amb els 3 dies d'evolució.
No hi ha crida AJAX que el refresqui: el servidor el renderitza *inline* i el JS només
canvia de dia al client (`actualitzaData()`).

### 3.1 Característiques mesurades (2026-08-05)

| Propietat | Valor |
| --- | --- |
| `cache-control` | `max-age=600` → **sondejar més sovint de 10 min no aporta res** |
| `ETag` / `Last-Modified` | **Absents** → no hi ha GET condicional possible |
| `vary` | `Accept-Encoding` (cal demanar gzip) |

Mides gzip de les pàgines que porten el payload complet:

| Pàgina | gzip |
| --- | ---: |
| `https://www.meteo.cat/observacions/radar` | ~57 KB |
| `https://www.meteo.cat/prediccio/municipal` | ~61 KB |
| `https://www.meteo.cat/prediccio/general` | ~67 KB |
| `https://www.meteo.cat/` | ~102 KB |

⚠️ En el mostreig fet, pàgines diferents han retornat conjunts d'episodis lleugerament
diferents. **Abans de fixar una pàgina, cal validar-la contra `https://www.meteo.cat/`
durant un episodi amb diversos meteors oberts** i, si hi ha dubte, quedar-se amb l'arrel.

### 3.2 Extracció

Localitzar `Meteocat.avisosSMP(` i, a partir d'allà, extreure els arrays de les claus
`avisos:` i `episodisPreavisos:` amb un comptador de claudàtors equilibrat (no amb una
expressió regular greedy: el payload conté claudàtors dins de cadenes). El resultat és JSON
vàlid tal qual (`json.loads`).

⚠️ Dins de la mateixa crida hi ha un objecte `opcions` que **també** té una clau `avisos`
(buida). Cal ancorar-se a la clau que conté un array no buit d'episodis, no a la primera
coincidència.

---

## 4. Referència territorial sense clau

### 4.1 TopoJSON de comarques ⭐

```
https://static-m.meteo.cat/assets-w3/json/topojson/comarquesAmbMar.json   (~58 KB)
https://static-m.meteo.cat/assets-w3/json/topojson/municipis.json         (~512 KB)
```

`comarquesAmbMar.json` és un `Topology` amb l'objecte `comarquesAmbMarCorrectes84`
(55 geometries) i propietats `{ "NOM_COMAR", "CAPCOMAR", "IDComarca" }` — **el mateix
`idComarca` que fa servir el payload d'avisos**. Serveix alhora de taula id→nom i de
geometria per a point-in-polygon.

`municipis.json` té propietats `{ "cm_ine_mun", "cm_nom_mun" }` (947 municipis) però **no**
porta la comarca, així que per resoldre ubicació → comarca cal fer point-in-polygon sobre
el fitxer de comarques.

### 4.2 Taula `IDComarca` (capturada 2026-08-05)

Versió llegible per màquina, per generar la taula estàtica del codi:
[`captures/comarques-idcomarca-2026-08-05.json`](captures/comarques-idcomarca-2026-08-05.json).

| id | Comarca | id | Comarca | id | Comarca |
| ---: | --- | ---: | --- | ---: | --- |
| 1 | Alt Camp | 16 | Conca de Barberà | 31 | Ripollès |
| 2 | Alt Empordà | 17 | Garraf | 32 | Segarra |
| 3 | Alt Penedès | 18 | Garrigues | 33 | Segrià |
| 4 | Alt Urgell | 19 | Garrotxa | 34 | Selva |
| 5 | Alta Ribagorça | 20 | Gironès | 35 | Solsonès |
| 6 | Anoia | 21 | Maresme | 36 | Tarragonès |
| 7 | Bages | 22 | Montsià | 37 | Terra Alta |
| 8 | Baix Camp | 23 | Noguera | 38 | Urgell |
| 9 | Baix Ebre | 24 | Osona | 39 | Val d'Aran |
| 10 | Baix Empordà | 25 | Pallars Jussà | 40 | Vallès Occidental |
| 11 | Baix Llobregat | 26 | Pallars Sobirà | 41 | Vallès Oriental |
| 12 | Baix Penedès | 27 | Pla d'Urgell | 42 | Moianès |
| 13 | Barcelonès | 28 | Pla de l'Estany | 43 | Lluçanès |
| 14 | Berguedà | 29 | Priorat | | |
| 15 | Cerdanya | 30 | Ribera d'Ebre | | |

Zones marítimes (`88 ≤ id ≤ 99`; el JS oficial les tracta com a cas especial):

| id | Zona | id | Zona |
| ---: | --- | ---: | --- |
| 88 | Mar Montsià | 94 | Mar Tarragonès |
| 89 | Mar Baix Ebre | 95 | Mar Barcelonès |
| 90 | Mar Baix Camp | 96 | Mar Maresme |
| 91 | Mar Baix Llobregat | 97 | Mar Selva |
| 92 | Mar Baix Penedès | 98 | Mar Baix Empordà |
| 93 | Mar Garraf | 99 | Mar Alt Empordà |

**Moianès (42) i Lluçanès (43) són comarques recents.** Qualsevol taula estàtica que
generem ha de tolerar ids desconeguts (§6), no petar.

---

## 5. Plans de Protecció Civil (CECAT) — recerca per a la integració germana

> **Fora d'abast de la v1 d'`ha-avisoscat`.** Aquesta font va a `ha-cecat`, una integració
> separada: l'àmbit és Catalunya sencera (no la comarca), l'abast natural cobreix plans no
> meteorològics (SISMICAT, TRANSCAT, RADCAT…) i el precedent de HA core és unànime
> (`nina` vs `dwd_weather_warnings`). Raonament complet a
> [`02-existing-integrations.md`](02-existing-integrations.md) §8. La recerca es conserva
> aquí perquè és vàlida i costa de refer.

```
GET https://analisi.transparenciacatalunya.cat/resource/wj9c-j6vf.json
```

Dataset **"Plans de protecció civil en fase de prealerta, alerta o emergència"** al portal
de Transparència (Socrata) — el mateix patró que `ha-incendiscat` ja fa servir per als
històrics d'incendis. Verificat en viu el 2026-08-05:

```json
[{ "plaicona": { "url": "https://documents.dadesobertes.gencat.cat/cecat/docs/ico_INUNCAT.png" },
   "plaacronim": "INUNCAT",
   "planom": "INUNCAT",
   "plafase": "ALERTA",
   "plaactivat": "SI",
   "fasedatahora": "05/08/2026 13:18",
   "comunicatpdf": { "url": "https://documents.dadesobertes.gencat.cat/cecat/docs/I-125912_ACTUALITZACIO--ACTIVAT_INUNCAT_202608051838.pdf" },
   "descripcio": "Avís intensitat pluja fins al 04/08  - " }]
```

Camps: `plaacronim` / `planom` (INUNCAT, VENTCAT, NEUCAT, PROCICAT, …), `plafase`
(`PREALERTA` / `ALERTA` / `EMERGÈNCIA`), `plaactivat` (`SI`/`NO`), `fasedatahora` (format
**`DD/MM/YYYY HH:MM`**, hora local, no ISO), `comunicatpdf.url`, `descripcio`.

**Per què val la pena**: un avís SMP diu què preveu el meteoròleg; aquest dataset diu si
Protecció Civil ha activat realment el pla. És oficial, obert, sense quota i en temps real.
Creuar les dues coses (`avís greu` **i** `INUNCAT en ALERTA`) és una condició d'automació
trivial amb les dues integracions instal·lades — no cal que comparteixin domini.

Notes: `descripcio` i `planom` són text lliure extern; la data no és ISO i pot venir buida;
la llista pot ser buida (cap pla actiu) — cap d'aquestes coses pot fer fallar el parseig.

---

## 6. Traps de tolerància obligatoris

Payload capturat en viu el **2026-08-05**, desat a
[`captures/smp-episodis-oberts-2026-08-05.json`](captures/smp-episodis-oberts-2026-08-05.json)
(fixture base per als tests):

```jsonc
[{ "estat": { "nom": "Obert", "data": null },
   "meteor": { "idMeteor": null, "nom": "Intensitat de pluja en 30 minuts" },
   "avisos": [{
     "tipus": "Avís",
     "estat": "Ampliat",
     "dataEmisio": "2026-08-04T15:30Z",
     "dataInici":  "2026-08-04T12:00Z",
     "dataFi":     "2026-08-06T17:59Z",
     "evolucions": [{
       "dia": "2026-08-04T00:00Z",
       "comentari": "Els xàfecs aniran acompanyats de tempesta i no es descarta fenòmens de temps violent, especialment pedra.",
       "representatiu": 1.0,
       "llindar1": "Intensitat > 20 mm / 30 minuts",
       "llindar2": null,
       "distribucioGeografica": "LOCAL",
       "valorMaxim": null,
       "periodes": [
         { "nom": "00-06", "afectacions": null },
         { "nom": "06-12", "afectacions": null },
         { "nom": "12-18", "afectacions": [
             { "dia": "2026-08-04T00:00Z", "llindar": "Intensitat > 20 mm / 30 minuts",
               "auxiliar": false, "perill": 2.0, "idComarca": 1.0, "nivell": 1.0 } ] },
         { "nom": "18-00", "afectacions": [ /* … */ ] } ] }] }] }]
```

Els preavisos tenen una **forma diferent** (sense comarca ni franges):

```json
{ "nivell": 1, "tipus": "Preavís",
  "dataInici": "2017-03-06T00:00Z", "dataFi": "2017-03-08T23:59Z",
  "dataEmisio": "2017-03-06T12:07Z", "estat": "Vigent",
  "llindar": "Calor intensa", "perill": 2, "comentari": "" }
```

| # | Trap | Regla |
| :---: | --- | --- |
| 1 | `estat` observat en viu com a **`"Ampliat"`**, però el JS oficial només compara amb `"Vigent"` | **Mai filtrar per literal.** Tractar com a actiu tot el que no estigui explícitament tancat i decidir la vigència amb `dataInici`/`dataFi` + la finestra de la franja |
| 2 | `perill`, `idComarca`, `nivell`, `representatiu` arriben com a **floats** (`2.0`) | Convertir amb `int(float(x))` tolerant, mai indexar per la clau crua |
| 3 | `afectacions` pot ser **`null`**, no `[]` | Llegir-ho com a cap entrada, conservant la franja; mai iterar-hi directament |
| 4 | Un episodi pot portar **diversos `avisos`** (emissions successives del mateix avís) | Desduplicar per (meteor, tipus) quedant-se el `dataEmisio` més recent; si empaten, el grau més alt |
| 5 | `meteor.nom` i els llindars són **text lliure en català** | Mapatge case-insensitive; si no es reconeix, `None` + warning conservant el nom cru; no `KeyError`, no `raise` |
| 6 | `idMeteor` és **`null`** al payload públic | No dependre'n mai com a clau |
| 7 | `idComarca` pot ser una comarca nova o una zona marítima | `comarques.nom(id)` retorna un nom llegible per defecte, amb warning i sense petar (§6 de [`04-architecture.md`](04-architecture.md)) |
| 8 | La darrera franja es diu **`"18-00"`** i no `"18-24"` | Fer servir el valor del JSON |
| 9 | El literal del tipus té variants històriques (`"Avís d'Observació"`, `"Avís temps violent"`) | Normalitzar amb prefixos/`casefold`, mai igualtat estricta |
| 10 | `comentari`, `llindar*`, `meteor.nom`, `descripcio` (CECAT) són **text extern no fiable** | Mai `allow_html`, mai interpolació HTML directa (regla de `CLAUDE.md`) |
| 11 | `fasedatahora` del CECAT és `DD/MM/YYYY HH:MM` local, no ISO | Parseig explícit tolerant; `None` si falla |

Aquesta és la mateixa disciplina que
[`ha-incendiscat`](https://github.com/pmontp19/ha-incendiscat/blob/main/docs/04-architecture.md)
§9 imposa per als FeatureServers ArcGIS: **`.get()` amb valor per defecte, mai indexació
directa**.

---

## 7. Resum de fiabilitat per font

| Font | Oficial? | Clau? | Freqüència útil | Risc | Ús |
| --- | :---: | :---: | --- | --- | --- |
| `meteo.cat` (payload inline) | Dades oficials, **accés no documentat** | No | 10 min (`max-age=600`) | Mitjà — el marcatge pot canviar sense avís | ⭐ Font principal |
| `api.meteo.cat/pronostic/v2/smp` | Sí | **Sí** | ~3 peticions/dia (quota ciutadana) | Baix | Opcional, quan l'usuari té clau |
| `static-m.meteo.cat/.../comarquesAmbMar.json` | Actiu estàtic oficial | No | Un cop (i cache) | Baix | Resolució comarca ⭐ |
| `analisi.transparenciacatalunya.cat/resource/wj9c-j6vf` | **Sí, dades obertes** | No | Contínua | Baix | `ha-cecat` (fora d'abast v1) |
| `api.meteo.cat/quotes/v1/consum-actual` | Sí | Sí | 1/dia | Baix | Sensor de diagnòstic |

**Cap dels endpoints sense clau és una API oficialment suportada.** Es tracten amb la
mateixa política que els FeatureServers d'`ha-incendiscat`: parseig tolerant, conservació
de l'últim estat bo, event de degradació i *repair issue* després de fallades persistents.

### Endpoints definitius per a la integració

```
# Avisos SMP sense clau (font principal)
https://www.meteo.cat/observacions/radar          # payload inline `Meteocat.avisosSMP(...)`
https://www.meteo.cat/                            # fallback, payload complet garantit

# Avisos SMP amb clau (opcional, capçalera x-api-key)
https://api.meteo.cat/pronostic/v2/smp/episodis-oberts?data={YYYY}-{MM}-{DD}Z
https://api.meteo.cat/pronostic/v1/smp/episodis-oberts/preavisos
https://api.meteo.cat/quotes/v1/consum-actual

# Referència territorial (sense clau)
https://static-m.meteo.cat/assets-w3/json/topojson/comarquesAmbMar.json

# Protecció Civil — NO a la v1, va a `ha-cecat` (sense clau, dades obertes)
https://analisi.transparenciacatalunya.cat/resource/wj9c-j6vf.json
```

---

## 8. Llicència i atribució

Les dades són propietat del **Servei Meteorològic de Catalunya**. Avís legal:
<https://www.meteo.cat/wpweb/avis-legal/>. La integració ha de:

- Declarar `ATTRIBUTION = "Dades del Servei Meteorològic de Catalunya (Meteocat)"`.
- Deixar clar al README que **no està afiliada ni aprovada** pel Meteocat ni per la
  Generalitat, i que la font sense clau no és una API oficialment suportada.
- No fer-ne ús comercial ni redistribuir-ne les dades.
