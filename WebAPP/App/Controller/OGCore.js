import { Message } from "../../Classes/Message.Class.js";
import { Ogc } from "../../Classes/Ogc.Class.js";
import { Model } from "../Model/OGCore.Model.js";

// The register derives country ids from the repo name (OG-ETH -> ETH); the
// vendored flag files use ISO2 (References/flags, see its README). Covers the
// register plus every OG country repo published so far. Unmapped countries
// get an icon fallback.
const FLAG_ISO2 = { ETH: 'et', ZAF: 'za', IDN: 'id', PHL: 'ph', USA: 'us', UK: 'gb', THA: 'th', BRA: 'br' };

// register values render into markup, escape them
const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g,
    ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));

export default class OGCore {
    static onLoad(){
        Ogc.getCalibrationCatalog()
        .then(response => {
            let model = new Model(response.countries, response.catalog_source);
            this.initPage(model);
        })
        .catch(error => {
            Message.danger(error);
            let model = new Model([], 'none');
            this.initPage(model);
        });
    }

    static initPage(model){
        Message.clearMessages();
        OGCore.renderGrid(model);
        OGCore.initEvents(model);
    }

    // One compact card per country. install_state drives the badge:
    // installed | update_available | not_installed | installing | failed.
    static renderGrid(model){
        $('#ogcGrid').empty();
        if (model.calibrations.length == 0){
            if (model.catalogSource == 'none'){
                $('#ogcEmptyTitle').text('The calibration catalogue is not reachable');
                $('#ogcEmptyText').text('Check the connection and reload the page.');
            }
            $('#ogcEmptyState').show();
            return;
        }
        $('#ogcEmptyState').hide();
        if (model.catalogSource == 'cache'){
            $('#ogcSourceNote').show();
        }
        $.each(model.calibrations, function (id, c) {
            let badge = {
                'installed': ['ogc-b-ok', 'installed'],
                'update_available': ['ogc-b-upd', 'update available'],
                'not_installed': ['ogc-b-mut', 'not installed'],
                'installing': ['ogc-b-run', 'installing...'],
                'failed': ['ogc-b-err', 'failed']
            }[c.install_state] || ['ogc-b-mut', c.install_state];
            let active = c.install_state == 'installed' || c.install_state == 'update_available';
            let iso2 = FLAG_ISO2[c.country_id];
            let flag = iso2
                ? `<img class="ogc-flag" src="References/flags/4x3/${iso2}.svg" alt="">`
                : `<span class="ogc-flag ogc-flag-none"><i class="fa fa-flag-o"></i></span>`;
            let card = `
            <div class="ogc-card ${active ? 'ogc-on' : ''}" data-country="${esc(c.country_id)}">
                <div class="ogc-card-head">
                    ${flag}
                    <div class="ogc-card-title">
                        <div class="ogc-card-name">${esc(c.country_name)}</div>
                        <div class="ogc-card-id">${esc(c.country_id)}</div>
                    </div>
                    <span class="ogc-badge ${badge[0]}">${esc(badge[1])}</span>
                </div>
                <div class="ogc-card-actions" data-state="${esc(c.install_state)}"></div>
            </div>`;
            $('#ogcGrid').append(card);
        });
    }

    static initEvents(model){
        // card actions (open, install, retry, add) attach here
    }
}
