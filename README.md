# Avisos Meteocat (`ha-avisoscat`)

Integració de Home Assistant per als **avisos de temps sever del Meteocat** (Situació
Meteorològica de Perill, SMP) a Catalunya.

> 🚧 **En construcció.** El disseny és a [`docs/`](docs/); el codi encara no hi és.

Segueix els avisos SMP que afecten la teva comarca i els converteix en entitats i events
per a automacions, distingint l'avís **anunciat** (emès amb hores o dies d'antelació) de
l'avís **en vigor** (la franja horària ja ha començat).

## Estat

| | |
| --- | --- |
| Domini de Home Assistant | `avisoscat` |
| Distribució | HACS (repositori personalitzat, de moment) |
| Font principal | `meteo.cat`, sense clau ni quota |
| Font opcional | `api.meteo.cat` amb clau pròpia |

## Integracions germanes

- [`ha-incendiscat`](https://github.com/pmontp19/ha-incendiscat) — incendis forestals i Pla Alfa.
- [`ha-cecat`](https://github.com/pmontp19/ha-cecat) — plans de Protecció Civil (INUNCAT, VENTCAT, …).

## Avís legal

Projecte no oficial, **no afiliat ni aprovat** pel Servei Meteorològic de Catalunya ni per
la Generalitat de Catalunya. Les dades són propietat del Servei Meteorològic de Catalunya
([avís legal](https://www.meteo.cat/wpweb/avis-legal/)).

Llicència [MIT](LICENSE).
