# Divinum Officium for Omarchy

An unofficial [Divinum Officium](https://www.divinumofficium.com/) client for the Omarchy bar. It keeps the traditional Roman canonical hours close at hand while leaving the liturgical texts to the open-source Divinum Officium project.

![Divinum Officium plugin preview](preview.png)

## Features

- A theme-colored Tatzenkreuz in the bar, with an optional current-hour label.
- All eight canonical hours in one panel, with the current hour highlighted and a countdown to the next.
- Direct links to the correct Office and Mass using the selected rubrics and languages.
- The day's feast or Sunday, rank, liturgical season and color, and commemorations.
- A feast-title link to the corresponding date in CatholicSaints.Info's public 1914 Roman Martyrology.
- Fixed custom hour boundaries or a sunrise-and-sunset schedule based on the location configured in Omarchy Weather.
- Six-hour metadata caching, with the most recent result retained for offline use.

### Settings

Click the gear in the panel's lower-right corner to choose the rubrics, primary and parallel languages, bar label, and hour schedule.

![Divinum Officium settings](screenshots/settings.png)

## Install

```bash
omarchy plugin add https://github.com/imr-git/omarchy-divinum-officium.git --enable
```

The widget defaults to the center section. To place it on the right:

```bash
omarchy bar move io.github.imr-git.divinum-officium --section right
```

## Usage

- Click the cross to open or close the panel.
- Middle-click the cross, or press `R` while the panel is focused, to refresh the liturgical metadata.
- Click an hour or **Mass of the Day** to open it on Divinum Officium.
- Click the feast title to open that date in the Roman Martyrology.
- Click the gear to edit settings; press `Escape` to close settings or the panel.

## Defaults

- Office version: `Tridentine - 1570`
- Primary language: Latin
- Parallel language: English
- Hour boundaries: Matins 00:00, Lauds 06:00, Prime 07:00, Terce 09:00, Sext 12:00, None 15:00, Vespers 18:00, Compline 21:00
- Schedule mode: fixed times

The optional solar schedule places Matins at the eighth hour of night, Lauds at civil dawn, Prime at sunrise, Terce/Sext/None at the quarter/middle/three-quarter points of daylight, Vespers at sunset, and Compline one hour later. It is a practical approximation: historical monastic timetables varied with season, latitude, work, meals, and the superior's judgment.

## Dependencies and network access

The plugin requires Omarchy's Quickshell bar and Python 3; it uses only Python's standard library. Daily metadata requests send the selected civil date, rubrical version, and languages to `www.divinumofficium.com`. Clicking an Office or Mass opens Divinum Officium in the configured browser.

Solar events are calculated locally from the coordinates already configured in Omarchy Weather. Those coordinates are not sent to Divinum Officium or another service by this plugin.

Results are cached under `${XDG_CACHE_HOME:-~/.cache}/omadivoff`.

## Remove

```bash
omarchy plugin remove io.github.imr-git.divinum-officium
```

The metadata cache is intentionally left in `${XDG_CACHE_HOME:-~/.cache}/omadivoff` and can be removed separately if desired.

## Development

```bash
omarchy plugin validate .
/usr/lib/qt6/bin/qmllint -I "$OMARCHY_PATH/shell" BarWidget.qml Panel.qml TatzenkreuzIcon.qml
python3 -m unittest discover -s tests -v
```

Omarchy plugin folders must not contain symlinks.

## Credits and affiliation

The liturgical texts, calendar, and web reader are provided by the MIT-licensed [Divinum Officium project](https://github.com/DivinumOfficium/divinum-officium). This plugin is unofficial, is not affiliated with Divinum Officium, and does not redistribute its liturgical texts.

Solar events use the [NOAA sunrise/sunset equations](https://gml.noaa.gov/grad/solcalc/solareqns.PDF). The traditional mapping is informed by the [Rule of Saint Benedict](https://archive.osb.org/rb/text/toc.html), especially chapters 8, 16, 41, 42, and 48.

## License

MIT
