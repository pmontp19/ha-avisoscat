# Elecció de la pàgina font del payload SMP — captura del 2026-08-06

Resol la pregunta oberta del §3.1 de [`../01-data-sources.md`](../01-data-sources.md): en el
mostreig del 2026-08-05 **pàgines diferents de `meteo.cat` van retornar conjunts d'episodis
lleugerament diferents**, i el §7 va deixar la pàgina primària i la de reserva com a dues
constants amb un comentari dient que el parser ho decidiria.

**Decisió: `https://www.meteo.cat/observacions/radar` es queda com a primària.** Les dues
pàgines candidates retornen el **mateix payload byte a byte**. La discrepància observada el
2026-08-05 no era entre pàgines: era entre les **dues crides** que l'arrel renderitza dins
d'una mateixa pàgina.

## Què hi havia al cel

Condició favorable per comparar: **episodi obert amb diversos meteors**, exactament el que
el §3.1 demanava abans de fixar una pàgina. No és una comparació en temps tranquil.

| | |
| --- | --- |
| `Avís Vigilància per Temps Violent` | **Vigent**, grau 6 (Cerdanya, Ripollès), emès a les 12:43Z, vigència fins a les 14:43Z |
| `Intensitat de pluja en 30 minuts` | Avís fins a grau 4 sobre 35–36 comarques, 3 dies d'evolució |
| `Intensitat de pluja en 3 hores` | Avís fins a grau 4 sobre 21 comarques |
| Preavisos | Cap |

## Mesures

Dues mostres aparellades, 10 minuts de separació, dues peticions cada una (`--compressed`,
`Accept-Encoding: gzip`). El *sha* és el SHA-256 del JSON extret, canonicalitzat amb claus
ordenades.

| Mostra | Pàgina | gzip | `cache-control` | Crides a la pàgina | Mides dels candidats | *sha* del payload triat |
| --- | --- | ---: | --- | :---: | --- | --- |
| 14:01Z | `/observacions/radar` | 57 KB | `max-age=180` | 1 | `[6]` | `42c7c35d66ef…` |
| 14:01Z | `/` (arrel) | 102 KB | `max-age=600` | **2** | `[3, 6]` | `42c7c35d66ef…` |
| 14:11Z | `/observacions/radar` | 57 KB | `max-age=180` | 1 | `[6]` | `51bdd9a23718…` |
| 14:11Z | `/` (arrel) | 102 KB | `max-age=600` | **2** | `[3, 6]` | `51bdd9a23718…` |

Cap capçalera `ETag` ni `Last-Modified` a cap de les dues, com ja deia el §3.1.

## Conclusions

1. **Les dues pàgines coincideixen exactament.** A cada mostra, el payload extret de les dues
   pàgines és idèntic (`sha` igual, i comparació camp a camp sense diferències). Guanya doncs
   la pàgina lleugera: ~57 KB contra ~102 KB cada 10 minuts.

2. **L'arrel renderitza la crida dues vegades i la primera és incompleta.** La crida del
   visor de portada porta `dies:1` i només els episodis d'avui (3 entrades); la del giny
   porta `dies:3` i els tres dies (6 entrades). El conjunt de la primera és un **subconjunt
   estricte** del de la segona. La crida de `dies:3` de l'arrel és idèntica a l'única crida
   de la pàgina del radar.

   Això és l'explicació de la discrepància del 2026-08-05: **ancorar-se a la primera
   coincidència** dona els episodis d'un sol dia si la pàgina és l'arrel, i els de tres dies
   si és el radar. Per això `parser.py` tria el candidat **més ric**, no el primer no buit, i
   llegeix les claus només al nivell superior de la crida.

3. **El `max-age` del radar és 180, no 600.** El §3.1 va mesurar 600 en una altra pàgina. El
   sòl de sondeig de 10 minuts del `const.py` continua sent correcte (és més conservador que
   el que la font demana), però la pàgina del radar es refresca més sovint del que suposàvem.

4. ⚠️ **L'ordre de `afectacions` no és estable entre peticions.** Entre les dues mostres les
   dades SMP **no van canviar gens** (mateix conjunt d'afectacions, mateixos graus, mateixos
   llindars) i tot i així el payload cru va canviar: la llista d'afectacions d'una franja surt
   **rotada** (mostra 1: comarques `1, 2, 3, …, 43`; mostra 2: `12, 13, …, 43, 1, 2, …, 11`).

   Conseqüència, **fora de l'abast del parser**: un `payload_hash` calculat sobre el
   payload cru canviarà a cada petició encara que res no hagi canviat, i això buida de
   sentit tant el `payload_hash` del §3 de `../04-architecture.md` com el
   `always_update=False` del §7. El hash ha de ser **insensible a l'ordre**; qui ho tanca
   avui és `models.compute_payload_hash()`, descrit al §3 de
   [`../04-architecture.md`](../04-architecture.md).

## Troballa col·lateral: l'avís de temps violent té una **tercera** forma

Fora de l'abast del parser, però la captura n'és la primera evidència en viu i afecta
`models.py`. El §6 en documentava dues (l'avís amb `evolucions` i el preavís pla); l'avís
de temps violent en fa servir una tercera: **`afectacions` penja directament de l'avís**, sense
`evolucions` ni `periodes`, coherent amb el fet que la seva vigència són 2 h des de
`dataEmisio` i no una franja de 6 h.

```jsonc
{ "tipus": "Avís Vigilància per Temps Violent",
  "comentari": "", "representatiu": "1",
  "llindar1": "Pedra de diàmetre > 2 cm, ratxes de vent > 90 km/h (25 m/s), …",
  "perill": 6.0,
  "dataInici": "2026-08-06T12:43Z", "dataFi": "2026-08-06T14:43Z",
  "dataEmisio": "2026-08-06T12:43Z", "estat": "Vigent",
  "afectacions": [ { "llindar": "…", "auxiliar": false,
                     "perill": 6.0, "idComarca": 15.0, "nivell": 2.0 } ] }
```

Nota també que `representatiu` arriba com a **cadena** (`"1"`), no com a float.

Efecte mesurat **el dia de la captura**: `parse_snapshot()` reconeixia l'episodi i el
tipus, però com que `_parse_avis()` només llegia `evolucions`, l'avís quedava amb
`evolucions=()` i **`perill_maxim == 0`**: un avís de temps violent **grau 6 vigent** sobre
la Cerdanya i el Ripollès arribava al model buit de contingut. El parser ja el lliurava
sencer (test a `tests/test_parser.py`). `models.py` ja llegeix aquesta forma; la regla i
l'estat vigents són al trap 12 del §6 de
[`../01-data-sources.md`](../01-data-sources.md).

## Reproducció

```sh
curl -sS --compressed -o radar.html https://www.meteo.cat/observacions/radar
curl -sS --compressed -o root.html  https://www.meteo.cat/
```

I després, amb `extract_smp_payload()` sobre cada fitxer, comparar el JSON extret amb les
claus ordenades. Ser educat amb un servei públic: unes poques peticions, mai un bucle.

## Payload capturat

El payload sencer de la mostra de les 14:01Z es conserva, verbatim, dins de la *fixture*
[`../../tests/fixtures/smp_page_sample.html`](../../tests/fixtures/smp_page_sample.html)
(pàgina del radar, retallada de tot el que no és el payload).
