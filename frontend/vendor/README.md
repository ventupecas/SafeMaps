# Libraries served with the page

Copied unchanged from npm (through unpkg.com), so the web map works without the internet:

| Folder | Package | Version | License |
| --- | --- | --- | --- |
| `leaflet/` | [leaflet](https://leafletjs.com) | 1.9.4 | BSD-2-Clause (`leaflet/LICENSE`) |
| `markercluster/` | [leaflet.markercluster](https://github.com/Leaflet/Leaflet.markercluster) | 1.5.3 | MIT (`markercluster/MIT-LICENCE.txt`) |
| `qrcode-generator/` | [qrcode-generator](https://github.com/kazuhikoarase/qrcode-generator) | 1.4.4 | MIT (in the file's header) |

To update one, download the same files from `https://unpkg.com/<package>@<version>/` and change the version here and in `maps.js` or `core.js`.
